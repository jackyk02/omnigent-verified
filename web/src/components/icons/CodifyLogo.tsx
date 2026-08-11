// The codify brand mark: one task fans out to parallel proposals, the
// verified winner carries forward into the check node. Shown on the
// new-session landing in place of the old mascot; inline SVG so it scales
// crisply and needs no asset fetch.
export function CodifyLogo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 128 128" role="img" aria-label="Codify" className={className} fill="none">
      <defs>
        <linearGradient id="codify-brand" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stopColor="#F43BA6" />
          <stop offset="1" stopColor="#FF7ACB" />
        </linearGradient>
      </defs>
      <path
        d="M18 64 C 40 64, 44 28, 66 28"
        stroke="#F43BA6"
        strokeOpacity="0.32"
        strokeWidth="7"
        strokeLinecap="round"
      />
      <path
        d="M18 64 C 40 64, 44 100, 66 100"
        stroke="#F43BA6"
        strokeOpacity="0.32"
        strokeWidth="7"
        strokeLinecap="round"
      />
      <path d="M18 64 L 66 64" stroke="url(#codify-brand)" strokeWidth="8" strokeLinecap="round" />
      <circle cx="70" cy="28" r="7" stroke="#F43BA6" strokeOpacity="0.38" strokeWidth="5" />
      <circle cx="70" cy="100" r="7" stroke="#F43BA6" strokeOpacity="0.38" strokeWidth="5" />
      <path d="M78 64 L 88 64" stroke="url(#codify-brand)" strokeWidth="8" strokeLinecap="round" />
      <circle cx="70" cy="64" r="8" fill="#F43BA6" />
      <circle cx="103" cy="64" r="17" fill="url(#codify-brand)" />
      <path
        d="M95.5 64.5 L 101 70 L 111 58.5"
        stroke="#FFFFFF"
        strokeWidth="5.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
