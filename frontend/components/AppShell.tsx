"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAuth } from "@/lib/auth-context";

const PUBLIC_PATHS = ["/login", "/register"];

/**
 * Chrome plus the client-side auth redirect.
 *
 * This is a convenience, not a security control: the API authorizes every
 * request independently, so a user who bypasses the redirect still cannot read
 * anything their role does not permit.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const { user, loading, logout, can } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isPublic = PUBLIC_PATHS.includes(pathname);

  useEffect(() => {
    if (!loading && !user && !isPublic) {
      router.replace("/login");
    }
  }, [loading, user, isPublic, router]);

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center text-sm text-slate-500">
        Loading…
      </div>
    );
  }

  if (isPublic) {
    return <main className="h-full">{children}</main>;
  }

  if (!user) return null;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-4 border-b border-slate-200 bg-white px-4 py-3">
        <Link href="/" className="font-semibold text-campus-900">
          Campus Knowledge Assistant
        </Link>

        <nav className="flex items-center gap-1 text-sm">
          <NavLink href="/" current={pathname}>
            Ask
          </NavLink>
          <NavLink href="/documents" current={pathname}>
            Documents
          </NavLink>
          {can("view_analytics") && (
            <NavLink href="/admin" current={pathname}>
              Admin
            </NavLink>
          )}
        </nav>

        <div className="ml-auto flex items-center gap-3 text-sm">
          <span className="hidden text-slate-600 sm:inline">{user.full_name}</span>
          <span className="rounded bg-campus-50 px-2 py-0.5 text-xs font-medium text-campus-700">
            {user.role}
          </span>
          <button
            type="button"
            onClick={logout}
            className="rounded px-2 py-1 text-slate-500 hover:bg-slate-100 hover:text-slate-700"
          >
            Sign out
          </button>
        </div>
      </header>

      <main className="min-h-0 flex-1">{children}</main>
    </div>
  );
}

function NavLink({
  href,
  current,
  children,
}: {
  href: string;
  current: string;
  children: React.ReactNode;
}) {
  const active = current === href;
  return (
    <Link
      href={href}
      className={`rounded px-2.5 py-1 transition ${
        active
          ? "bg-campus-50 font-medium text-campus-700"
          : "text-slate-600 hover:bg-slate-100"
      }`}
    >
      {children}
    </Link>
  );
}
