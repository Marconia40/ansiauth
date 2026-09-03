import { PlaceholderPanel } from '@/components/scope/Panel';

// Wired into the session-menu dropdown in the topbar. Real form (current
// password + new + confirm) lands in block 6 alongside the users pass.
export default function ChangePasswordPage() {
  return (
    <div className="flex flex-col gap-3 max-w-md">
      <h1 className="text-xl font-semibold text-text">Change password</h1>
      <PlaceholderPanel label="Change password form (block 6)" />
    </div>
  );
}
