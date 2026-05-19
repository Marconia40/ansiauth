import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// Guard all dashboard routes behind a session cookie.
// The cookie is set by the login page after a successful API login
// and cleared on logout. It carries no credential — it is a presence flag only.
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isLoggedIn = request.cookies.has('session');
  const isAuthRoute = pathname === '/login';

  if (!isLoggedIn && !isAuthRoute) {
    return NextResponse.redirect(new URL('/login', request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico).*)'],
};
