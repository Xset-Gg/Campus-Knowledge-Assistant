import type { Metadata } from "next";

import "./globals.css";
import { AuthProvider } from "@/lib/auth-context";
import { AppShell } from "@/components/AppShell";

export const metadata: Metadata = {
  title: "Campus Knowledge Assistant",
  description:
    "Answers to university policy and course questions, cited to the exact page of the official document.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="h-full">
      <body className="h-full">
        <AuthProvider>
          <AppShell>{children}</AppShell>
        </AuthProvider>
      </body>
    </html>
  );
}
