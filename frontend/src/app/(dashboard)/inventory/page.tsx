import Link from 'next/link';
import { PlaceholderPanel } from '@/components/scope/Panel';

// Block 1 placeholder — the real inventory (device registration + list) lives at
// /devices for now; block 6 will move it fully to this path and give it the new
// styling. Linking to /devices lets the user reach the working page in the
// meantime.
export default function InventoryPage() {
  return (
    <div className="flex flex-col gap-3">
      <h1 className="text-xl font-semibold text-text">Inventory</h1>
      <PlaceholderPanel label="Device inventory (registration + list) — styled in block 6" />
      <p className="text-sm text-muted">
        The existing management screen is still available at{' '}
        <Link href="/devices" className="text-info underline">
          /devices
        </Link>
        .
      </p>
    </div>
  );
}
