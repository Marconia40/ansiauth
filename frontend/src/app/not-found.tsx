import Link from 'next/link';

export default function NotFound() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-50">
      <div className="text-center">
        <p className="text-5xl font-bold text-gray-300 mb-4">404</p>
        <h2 className="text-xl font-semibold text-gray-800 mb-2">Page not found</h2>
        <p className="text-gray-500 mb-6">The page you&#39;re looking for doesn&#39;t exist.</p>
        <Link href="/" className="text-blue-600 hover:underline text-sm">
          Go to Dashboard
        </Link>
      </div>
    </div>
  );
}
