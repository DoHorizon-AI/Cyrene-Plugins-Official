// -----------------------------------------------------------------------------
// Module: src/main.tsx
// Role: Browser entry point for the Navigator React console.
// -----------------------------------------------------------------------------
// 中文：// 中文：模块职责：Navigator React 控制台的浏览器入口。

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
