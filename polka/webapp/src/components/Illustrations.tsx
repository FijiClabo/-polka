// Спокойные линейные иллюстрации в «книжном» стиле: тонкая линия, приглушённые акценты, мягкое пятно фоном.
// Цвета берутся из токенов темы, поэтому рисунки сами подстраиваются под светлую и тёмную тему.

const line = { fill: "none", strokeWidth: 1.6, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
const ink = { ...line, stroke: "var(--text)", strokeOpacity: 0.75 };
const c = (v: string) => ({ ...line, stroke: `var(${v})` });

export function BookStackIll({ size = 170 }: { size?: number }) {
  return (
    <svg width={size} height={(size * 120) / 160} viewBox="0 0 160 120" aria-hidden="true">
      <circle cx="82" cy="60" r="44" fill="var(--accent-soft)" />
      <path d="M18 101h124" {...ink} strokeOpacity={0.3} />
      <rect x="34" y="85" width="92" height="16" rx="2" {...c("--blue")} />
      <path d="M41 85v16M120 89H62" {...c("--blue")} />
      <rect x="44" y="71" width="76" height="14" rx="2" {...c("--accent")} />
      <path d="M50 71v14M114 78H70" {...c("--accent")} />
      <rect x="38" y="59" width="68" height="12" rx="2" {...ink} />
      <path d="M44 59v12" {...ink} />
      <path d="M86 45h20v7a10 10 0 0 1-20 0z" {...ink} />
      <path d="M106 48h3a4 4 0 0 1 0 8h-4.5" {...ink} />
      <path d="M92 38c-2-3 2-5 0-8M100 38c-2-3 2-5 0-8" {...ink} strokeOpacity={0.4} />
      <path d="M24 101c0-12 6-20 14-24M30 88c-5-1-8-4-9-8 5 0 8 3 9 8ZM34 80c-1-5 1-9 5-11 1 5-1 9-5 11Z" {...c("--green")} />
    </svg>
  );
}

export function OpenBookIll({ size = 170 }: { size?: number }) {
  return (
    <svg width={size} height={(size * 120) / 160} viewBox="0 0 160 120" aria-hidden="true">
      <circle cx="80" cy="58" r="44" fill="var(--mustard-soft)" />
      <path d="M80 36c-14-9-34-10-52-6v58c18-4 38-3 52 6 14-9 34-10 52-6V30c-18-4-38-3-52 6Z" {...ink} />
      <path d="M80 36v58" {...ink} />
      <path d="M40 48c10-2 20-1 30 3M40 60c10-2 20-1 30 3M40 72c10-2 20-1 30 3M90 51c10-4 20-5 30-3M90 63c10-4 20-5 30-3" {...c("--blue")} strokeWidth={1.2} />
      <path d="M58 24c4-3 8-3 12 0M90 24c4-3 8-3 12 0" {...c("--accent")} />
    </svg>
  );
}

export function CupIll({ size = 150 }: { size?: number }) {
  return (
    <svg width={size} height={(size * 120) / 160} viewBox="0 0 160 120" aria-hidden="true">
      <circle cx="80" cy="62" r="42" fill="var(--sage-soft)" />
      <path d="M30 98h100" {...ink} strokeOpacity={0.3} />
      <path d="M54 58h46v14a23 23 0 0 1-46 0z" {...ink} />
      <path d="M100 63h5a8 8 0 0 1 0 16h-7" {...ink} />
      <path d="M46 98c6-4 14-4 20 0M94 98c6-4 14-4 20 0" {...ink} strokeOpacity={0.35} />
      <path d="M68 48c-3-4 3-7 0-11M78 48c-3-4 3-7 0-11M88 48c-3-4 3-7 0-11" {...c("--accent")} />
    </svg>
  );
}
