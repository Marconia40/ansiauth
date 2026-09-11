import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';

// Guard all dashboard routes behind the httpOnly refresh_token cookie set by the backend.
// The backend sets it on login and clears it on logout — the frontend never touches it.
export function proxy(request: NextRequest) {
  const { pathname } = request.nextUrl;
  const isLoggedIn = request.cookies.has('refresh_token');
  const isAuthRoute = pathname === '/login';

  if (!isLoggedIn && !isAuthRoute) {
    return NextResponse.redirect(new URL('/login', request.url));
  }

  return NextResponse.next();
}

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon.ico|icon.svg|brand/).*)'],
};
