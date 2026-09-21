// -----------------------------------------------------------------------------
// Module: src/main.tsx
// Role: Browser entry point for the Navigator React console.
// -----------------------------------------------------------------------------

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) {
  throw new Error("Navigator UI root element is missing.");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
