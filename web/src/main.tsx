import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Route, Routes } from "react-router-dom";
import Layout from "./Layout";
import AnomalyPage from "./pages/AnomalyPage";
import ClipPage from "./pages/ClipPage";
import ClipsPage from "./pages/ClipsPage";
import EquipmentPage from "./pages/EquipmentPage";
import EvalsPage from "./pages/EvalsPage";
import IncidentsPage from "./pages/IncidentsPage";
import TracePage from "./pages/TracePage";
import TracesPage from "./pages/TracesPage";
import UploadPage from "./pages/UploadPage";
import "./styles.css";

// HashRouter: FastAPI serves the built app as static files, so deep links (/#/clips/...) never 404.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<IncidentsPage />} />
          <Route path="clips" element={<ClipsPage />} />
          <Route path="clips/:clipId" element={<ClipPage />} />
          <Route path="anomalies/:anomalyId" element={<AnomalyPage />} />
          <Route path="upload" element={<UploadPage />} />
          <Route path="equipment" element={<EquipmentPage />} />
          <Route path="traces" element={<TracesPage />} />
          <Route path="traces/:runId" element={<TracePage />} />
          <Route path="evals" element={<EvalsPage />} />
          <Route path="*" element={<div className="empty">Page not found.</div>} />
        </Route>
      </Routes>
    </HashRouter>
  </React.StrictMode>,
);
