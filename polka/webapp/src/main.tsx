import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

// Шрифты из макета: Unbounded — цифры и заголовки, Onest — интерфейс,
// Cormorant — названия и обложки, Literata — чтение. Загружаются только нужные начертания и алфавиты.
import "@fontsource-variable/onest/index.css";
import "@fontsource-variable/unbounded/wght.css";
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
