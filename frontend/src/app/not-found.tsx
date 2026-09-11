import Link from 'next/link';
import { Brand } from '@/components/Brand';

export default function NotFound() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-app text-text">
      <div className="text-center">
        <Brand
          variant="iso"
          tone="white"
          className="h-20 w-auto mx-auto mb-4 opacity-70"
        />
        <p className="text-5xl font-bold text-muted mb-2">404</p>
        <h2 className="text-xl font-semibold mb-2">Page not found</h2>
        <p className="text-muted mb-6">
          The page you&#39;re looking for doesn&#39;t exist.
        </p>
        <Link href="/" className="text-info hover:underline text-sm">
          Go to Dashboard
        </Link>
      </div>
    </div>
  );
}
