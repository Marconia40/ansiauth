import { Brand } from './Brand';

interface Props {
  size?: 'sm' | 'md' | 'lg';
}

const sizes = {
  sm: 'h-4 w-4',
  md: 'h-8 w-8',
  lg: 'h-12 w-12',
};

export function LoadingSpinner({ size = 'md' }: Props) {
  return (
    <div className="flex items-center justify-center">
      <Brand
        variant="iso"
        tone="color"
        className={`${sizes[size]} animate-pulse`}
        title="Loading"
      />
    </div>
  );
}
