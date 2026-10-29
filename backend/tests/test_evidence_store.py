"""Blob evidence store against an in-memory stand-in for azure.storage.blob.ContainerClient."""

import pytest

from backend.app.config import missing_settings
from backend.app.evidence.store import BlobEvidenceStore, LocalEvidenceStore, make_evidence_store

from .fakes import make_settings


class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.content_types: dict[str, str] = {}

    def upload_blob(self, name, data, overwrite=False, content_settings=None):
        assert overwrite
        self.blobs[name] = bytes(data)
        self.content_types[name] = content_settings.content_type

    def download_blob(self, name):
        data = self.blobs[name]
        return type("Downloader", (), {"readall": lambda self: data})()

    def get_blob_client(self, name):
        blobs = self.blobs
        return type("BlobClient", (), {"exists": lambda self: name in blobs})()


def test_writes_go_to_the_container_with_a_content_type_and_to_the_local_cache(tmp_path):
    container = FakeContainer()
    store = BlobEvidenceStore(container, tmp_path)
    uri = store.write(f"{store.clip_dir('c1')}/frames/f00001.jpg", b"jpeg")
    assert uri == "clip=c1/frames/f00001.jpg"
    assert container.blobs[uri] == b"jpeg" and container.content_types[uri] == "image/jpeg"
    assert (tmp_path / uri).read_bytes() == b"jpeg"


def test_a_cache_miss_downloads_the_blob(tmp_path):
    container = FakeContainer()
    container.blobs["clip=c1/source.mp4"] = b"video"
    store = BlobEvidenceStore(container, tmp_path / "cache")
    assert store.exists("clip=c1/source.mp4")
    path = store.path("clip=c1/source.mp4")                 # OpenCV / SAM / FileResponse need a real file
    assert path.read_bytes() == b"video"
    assert store.read("clip=c1/source.mp4") == b"video"
    assert not store.exists("clip=c1/missing.png")
    assert not store.path("clip=c1/missing.png").is_file()


def test_paths_cannot_escape_the_store(tmp_path):
    store = BlobEvidenceStore(FakeContainer(), tmp_path)
    with pytest.raises(ValueError):
        store.write("../outside.png", b"x")
    with pytest.raises(ValueError):
        store.path("../../etc/passwd")


def test_settings_choose_the_store(tmp_path):
    assert isinstance(make_evidence_store(make_settings(tmp_path, evidence_dir=tmp_path)), LocalEvidenceStore)
    blob = make_settings(tmp_path, evidence_dir=tmp_path, evidence_store="blob")
    assert missing_settings(blob) == ["AZURE_STORAGE_CONNECTION_STRING"]
    with pytest.raises(ValueError):
        make_evidence_store(blob)
