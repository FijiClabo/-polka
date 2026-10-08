import { useState } from "react";
import { IFlameSolid } from "../components/Icons";
import { Avatar, Shelf } from "../components/ui";
import { haptic } from "../tg";

const SLIDES = [
  {
    title: "Своя книга.\nСвой план.",
    text: "Загрузи epub или fb2 — или читай без файла, по своему изданию. Разобьём на отрезки по 15 минут в день и поставим финиш.",
    ill: () => (
      <div style={{ width: 260 }}>
        <Shelf
          items={[
            { id: 1, color: "#E2B84F", pages: 260 },
            { id: 2, color: "#7E9C7A", pages: 420 },
            { id: 3, color: "#D49A8C", pages: 520 },
            { id: 4, color: "#4F6F9F", pages: 380 },
            { id: 5, color: "#C9644F", pages: 300, reading: true, progress: 0.4 },
          ]}
          height={210}
        />
      </div>
    ),
  },
  {
    title: "Перескажи\nза минуту",
    text: "Прочитай отрезок и расскажи голосом или текстом, что там было. День засчитывается только после пересказа.",
    ill: () => (
      <div className="col" style={{ alignItems: "center" }}>
        <div className="rec-btn on" style={{ width: 120, height: 120 }}>
          <svg width="44" height="44" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <rect x="9" y="3" width="6" height="11" rx="3" fill="currentColor" />
            <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
          </svg>
        </div>
        <div className="wave" style={{ marginTop: 34 }}>
          {[10, 22, 34, 18, 44, 28, 52, 30, 20, 38, 14, 26, 46, 22, 12].map((h, i) => (
            <i key={i} style={{ height: h }} />
          ))}
        </div>
      </div>
    ),
  },
  {
    title: "Стрик\nс напарником",
    text: "Дни подряд — это стрик. С напарником он общий: растёт, только если день сдан у вас двоих. Подводить друг друга не хочется.",
    ill: () => (
      <div className="row" style={{ gap: 18 }}>
        <Avatar name="Ты" size={88} seed={0} ring="grey" />
        <div className="col" style={{ alignItems: "center", gap: 2 }}>
          <IFlameSolid size={34} />
          <span className="num" style={{ fontSize: 48, lineHeight: 1 }}>7</span>
        </div>
        <Avatar name="Аня" size={88} seed={1} ring="green" />
      </div>
    ),
  },
];

export default function Welcome({ onDone }: { onDone: () => void }) {
  const [i, setI] = useState(0);
  const s = SLIDES[i];
  const last = i === SLIDES.length - 1;
  return (
    <div className="welcome" key={i}>
      <div className="ill" style={{ animation: "screen-in .4s var(--ease)" }}>{s.ill()}</div>
      <h2 style={{ whiteSpace: "pre-line" }}>{s.title}</h2>
      <p>{s.text}</p>
      <div className="dots">
        {SLIDES.map((_, k) => (
          <i key={k} className={k === i ? "on" : ""} />
        ))}
      </div>
      <button
        className="btn primary block"
        onClick={() => {
          haptic("light");
          if (last) onDone();
          else setI(i + 1);
        }}
      >
        {last ? "Начать" : "Дальше"}
      </button>
      {!last && (
        <button className="btn ghost block mt-8" onClick={onDone}>
          Пропустить
        </button>
      )}
    </div>
  );
}
