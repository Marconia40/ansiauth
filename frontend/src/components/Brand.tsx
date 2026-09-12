type Variant = 'iso' | 'wordmark';
type Tone = 'color' | 'white' | 'black';

interface Props {
  variant?: Variant;
  tone?: Tone;
  /** Wordmark only — use the crop that trims the SVG's built-in vertical whitespace. */
  tight?: boolean;
  className?: string;
  title?: string;
}

const FILE: Record<Variant, Record<Tone, string>> = {
  iso: {
    color: '/brand/AnsiAuth_Iso_Color.svg',
    white: '/brand/AnsiAuth_Iso_Blanco.svg',
    black: '/brand/AnsiAuth_Iso_Negro.svg',
  },
  wordmark: {
    color: '/brand/AnsiAuth_Logo_Color.svg',
    white: '/brand/AnsiAuth_Logo_Blanco.svg',
    black: '/brand/AnsiAuth_Logo_Negro.svg',
  },
};

const WORDMARK_TIGHT: Record<Tone, string> = {
  color: '/brand/AnsiAuth_Logo_Color_Tight.svg',
  white: '/brand/AnsiAuth_Logo_Blanco_Tight.svg',
  black: '/brand/AnsiAuth_Logo_Negro.svg',
};

export function Brand({
  variant = 'wordmark',
  tone = 'color',
  tight = false,
  className,
  title = 'AnsiAuth',
}: Props) {
  const src =
    tight && variant === 'wordmark' ? WORDMARK_TIGHT[tone] : FILE[variant][tone];
  return (
    // next/image skips optimization for SVG and forces width/height — plain img is simpler here.
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt={title} className={className} draggable={false} />
  );
}
