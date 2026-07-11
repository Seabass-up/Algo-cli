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
    description: "Tools, durable context, routed agents, and explicit verification in one local-first runtime.",
    url: "https://algo-cli.com",
    images: [{ url: "/og.png", width: 1660, height: 920, alt: "Algo CLI — Verified work. Local control." }],
  },
  twitter: { card: "summary_large_image", title: "Algo CLI — Verified work. Local control.", description: "A local-first agent runtime built around verified work.", images: ["/og.png"] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>;
}
