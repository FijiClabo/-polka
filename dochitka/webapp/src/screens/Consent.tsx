import { useState } from "react";
import { api, type Me } from "../api";
import { BookStackIll } from "../components/Illustrations";
import { toast } from "../components/ui";
import { haptic, openExternal } from "../tg";

const POINTS = [
  "Имя и id в Telegram, часовой пояс, книги и прогресс — чтобы строить план и напоминать о чтении.",
  "Пересказы и голосовые не храним: они нужны только для проверки и сразу удаляются.",
  "Напарник видит лишь твой прогресс, друзья — книгу, стрик, значки и полку.",
  "Отозвать согласие и удалить всё можно в любой момент — командой /delete_me в чате с ботом.",
];

export default function Consent({ me, onDone }: { me: Me; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const docs = me.docs;
  return (
    <div className="welcome consent">
      <div className="ill"><BookStackIll size={190} /></div>
      <h2>Последний шаг</h2>
      <p>Чтобы вести твой план и проверять пересказы, нужно согласие на обработку данных.</p>
      <ul className="consent-list">
        {POINTS.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
      {(docs.consent || docs.privacy) && (
        <p className="tiny muted">
          Документы:{" "}
          {docs.consent && <button className="link tiny" onClick={() => openExternal(docs.consent!)}>согласие на обработку данных</button>}
          {docs.consent && docs.privacy && " и "}
          {docs.privacy && <button className="link tiny" onClick={() => openExternal(docs.privacy!)}>политика конфиденциальности</button>}
          .
        </p>
      )}
      <button
        className="btn primary block"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          haptic("light");
          try {
            await api.post("/consent");
            onDone();
          } catch (e) {
            toast((e as Error).message);
            setBusy(false);
          }
        }}
      >
        Даю согласие
      </button>
    </div>
  );
}
