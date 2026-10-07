import type { SVGProps } from "react";

type P = SVGProps<SVGSVGElement> & { size?: number };

const base = (size = 24) => ({
  width: size,
  height: size,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.8,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
});

export const IFlame = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M12 21c-3.9 0-6.5-2.6-6.5-6.1 0-2.6 1.5-4.4 3-6 .9-1 1.8-2.1 2.3-3.9.1-.5.7-.6 1-.2 2.8 3.4 6.7 5.6 6.7 10.1 0 3.5-2.6 6.1-6.5 6.1Z" />
    <path d="M12 21c-1.7 0-2.9-1.1-2.9-2.8 0-1.8 1.4-2.8 2.4-4.1.2-.3.6-.2.7.1.7 1.5 2.7 2.3 2.7 4 0 1.7-1.2 2.8-2.9 2.8Z" />
  </svg>
);

export const IFlameSolid = ({ size = 18, ...p }: P) => (
  <svg width={size} height={size} viewBox="0 0 24 24" {...p}>
    <defs>
      <linearGradient id="fl" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stopColor="#FFC15A" />
        <stop offset="1" stopColor="#FF5F33" />
      </linearGradient>
    </defs>
    <path
      fill="url(#fl)"
      d="M12 22c-4.2 0-7-2.8-7-6.6 0-2.8 1.6-4.8 3.2-6.5 1-1.1 2-2.3 2.5-4.2.1-.6.8-.7 1.1-.2 3 3.6 7.2 6 7.2 10.9 0 3.8-2.8 6.6-7 6.6Z"
    />
    <path fill="#FFE2A8" d="M12 22c-1.8 0-3.1-1.2-3.1-3 0-2 1.5-3 2.6-4.4.2-.3.7-.2.8.1.8 1.6 2.8 2.5 2.8 4.3 0 1.8-1.3 3-3.1 3Z" />
  </svg>
);

export const ICalendar = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <rect x="3.5" y="5" width="17" height="15.5" rx="3" />
    <path d="M3.5 10h17M8 3v4M16 3v4" />
  </svg>
);

export const IPeople = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <circle cx="9" cy="8.5" r="3.3" />
    <path d="M3 19.5c.6-3.2 3-5 6-5s5.4 1.8 6 5" />
    <path d="M15.5 5.6a3.2 3.2 0 0 1 0 6.1M17.5 14.8c1.8.6 3.1 2.2 3.5 4.7" />
  </svg>
);

export const IBooks = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <rect x="3.5" y="4" width="4" height="16" rx="1.2" />
    <rect x="9" y="4" width="4" height="16" rx="1.2" />
    <path d="m15 5.2 3.6-1 3 14.8-3.6 1Z" />
  </svg>
);

export const ICheck = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2.4} {...p}>
    <path d="m5 12.5 4.5 4.5L19 7.5" />
  </svg>
);

export const IClose = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2.2} {...p}>
    <path d="M6 6l12 12M18 6 6 18" />
  </svg>
);

export const IBack = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2.2} {...p}>
    <path d="M15 5 8 12l7 7" />
  </svg>
);

export const IArrow = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2.2} {...p}>
    <path d="M5 12h14M13 6l6 6-6 6" />
  </svg>
);

export const IChevron = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2} {...p}>
    <path d="m9 6 6 6-6 6" />
  </svg>
);

export const IPlus = ({ size, ...p }: P) => (
  <svg {...base(size)} strokeWidth={2.2} {...p}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);

export const IRefresh = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M4.5 12a7.5 7.5 0 1 0 2.2-5.3" />
    <path d="M4 4.5v4h4" />
  </svg>
);

export const IMic = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <rect x="9" y="3" width="6" height="11" rx="3" />
    <path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" />
  </svg>
);

export const ILock = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <rect x="5" y="10.5" width="14" height="10" rx="2.5" />
    <path d="M8 10.5V8a4 4 0 0 1 8 0v2.5" />
  </svg>
);

export const IShare = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M12 15V3.5M7.5 8 12 3.5 16.5 8" />
    <path d="M5 12.5v5.5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5.5" />
  </svg>
);

export const ISnow = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9M9.5 4.5 12 7l2.5-2.5M9.5 19.5 12 17l2.5 2.5" />
  </svg>
);

export const IBook = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v15H6.5A2.5 2.5 0 0 0 4 20.5Z" />
    <path d="M4 20.5A2.5 2.5 0 0 1 6.5 18H20v3H6.5" />
  </svg>
);

export const IHand = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M8 12V6.5a1.5 1.5 0 0 1 3 0V11M11 10.5V5a1.5 1.5 0 0 1 3 0v6M14 10.5V6.5a1.5 1.5 0 0 1 3 0V14c0 4-2.6 7-6.5 7-2.6 0-4-1-5.4-3L3.6 15a1.5 1.5 0 0 1 2.3-1.9L8 15" />
  </svg>
);

export const ISettings = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <circle cx="12" cy="12" r="3" />
    <path d="M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-1.8-.3 1.6 1.6 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.6 1.6 0 0 0-1-1.5 1.6 1.6 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.6 1.6 0 0 0 .3-1.8 1.6 1.6 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.6 1.6 0 0 0 1.5-1 1.6 1.6 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9a1.6 1.6 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.6 1.6 0 0 0 1 1.5 1.6 1.6 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8V9a1.6 1.6 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.6 1.6 0 0 0-1.5 1Z" />
  </svg>
);

export const IUpload = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M12 16V4M7 9l5-5 5 5" />
    <path d="M4 15v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3" />
  </svg>
);

export const IText = ({ size, ...p }: P) => (
  <svg {...base(size)} {...p}>
    <path d="M4 7V5h16v2M12 5v14M9 19h6" />
  </svg>
);
