import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  metadataBase: new URL("https://algo-cli.com"),
  title: { default: "Algo CLI — Verified work. Local control.", template: "%s · Algo CLI" },
  description: "A local-first agent runtime for tools, durable context, routed agents, and verified work.",
  applicationName: "Algo CLI",
  keywords: ["agent runtime", "CLI", "Ollama", "local AI", "coding agent", "RAG"],
  openGraph: {
    type: "website",
    siteName: "Algo CLI",
    title: "Algo CLI — Verified work. Local control.",
    description: "Verified coding with bounded tool context: 73.2–74.3% fewer schema tokens in two deterministic coding scenarios.",
    url: "https://algo-cli.com",
    images: [{ url: "/og-v2.png", width: 1683, height: 935, alt: "Algo CLI coding benchmark evidence." }],
  },
  twitter: { card: "summary_large_image", title: "Algo CLI — Verified work. Local control.", description: "73.2–74.3% fewer tool-schema tokens in two deterministic coding scenarios.", images: ["/og-v2.png"] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
