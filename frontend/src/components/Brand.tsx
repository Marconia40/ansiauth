type Variant = 'iso' | 'wordmark';
type Tone = 'color' | 'white' | 'black';

interface Props {
  variant?: Variant;
  tone?: Tone;
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

export function Brand({
  variant = 'wordmark',
  tone = 'color',
  className,
  title = 'AnsiAuth',
}: Props) {
  return (
    // next/image skips optimization for SVG and forces width/height — plain img is simpler here.
    // eslint-disable-next-line @next/next/no-img-element
    <img
      src={FILE[variant][tone]}
      alt={title}
      className={className}
      draggable={false}
    />
  );
}
