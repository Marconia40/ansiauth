import { redirect } from 'next/navigation';

// The devices management screen lives at /inventory now. This stub keeps
// existing bookmarks and internal links working.
export default function DevicesRedirectPage() {
  redirect('/inventory');
}
