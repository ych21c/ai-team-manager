import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "AI Team Manager",
  description: "프로젝트마다 독립된 AI 개발팀을 자동으로 구성하고 관리합니다.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body style={{ margin: 0, padding: 0 }}>{children}</body>
    </html>
  );
}
