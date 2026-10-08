import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Шрифты: Onest — интерфейс, Cormorant — заголовки, цифры и названия книг, Literata — чтение.
// Загружаются только нужные начертания и алфавиты.
import "@fontsource-variable/onest/index.css";
import "@fontsource/cormorant/cyrillic-500.css";
import "@fontsource/cormorant/latin-500.css";
import "@fontsource/cormorant/cyrillic-600.css";
import "@fontsource/cormorant/latin-600.css";
import "@fontsource/cormorant/cyrillic-500-italic.css";
import "@fontsource/cormorant/latin-500-italic.css";
import "@fontsource/cormorant/cyrillic-600-italic.css";
import "@fontsource/cormorant/latin-600-italic.css";
import "@fontsource/literata/cyrillic-400.css";
import "@fontsource/literata/latin-400.css";
import "@fontsource/literata/cyrillic-400-italic.css";

import "./styles/tokens.css";
import "./styles/app.css";
import App from "./App";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
