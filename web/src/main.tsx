import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter, Route, Routes } from "react-router-dom";
import { AuthProvider } from "./auth";
import Layout from "./Layout";
import AccountPage from "./pages/AccountPage";
import { AdminOnly } from "./components";
import PeoplePage from "./pages/PeoplePage";
import AnomalyPage from "./pages/AnomalyPage";
import ClipPage from "./pages/ClipPage";
import ClipsPage from "./pages/ClipsPage";
import ScanPage from "./pages/ScanPage";
import EquipmentPage from "./pages/EquipmentPage";
import EvalsPage from "./pages/EvalsPage";
import HomePage from "./pages/HomePage";
import IncidentsPage from "./pages/IncidentsPage";
import TracePage from "./pages/TracePage";
import TracesPage from "./pages/TracesPage";
import UploadPage from "./pages/UploadPage";
import "./styles.css";

// HashRouter: FastAPI serves the built app as static files, so deep links (/#/clips/...) never 404.
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthProvider>
    <HashRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<HomePage />} />
          <Route path="incidents" element={<IncidentsPage />} />
          <Route path="clips" element={<ClipsPage />} />
          <Route path="clips/:clipId" element={<ClipPage />} />
          <Route path="anomalies/:anomalyId" element={<AnomalyPage />} />
          <Route path="upload" element={<UploadPage />} />
          <Route path="scan" element={<ScanPage />} />
          <Route path="equipment" element={<EquipmentPage />} />
          <Route path="account" element={<AccountPage />} />
          <Route path="people" element={<AdminOnly><PeoplePage /></AdminOnly>} />
          <Route path="traces" element={<AdminOnly><TracesPage /></AdminOnly>} />
          <Route path="traces/:runId" element={<TracePage />} />
          <Route path="evals" element={<AdminOnly><EvalsPage /></AdminOnly>} />
          <Route path="*" element={<div className="empty">Page not found.</div>} />
        </Route>
      </Routes>
    </HashRouter>
    </AuthProvider>
  </React.StrictMode>,
);
