// vitest の最小設定（設計 v2 §1.4）。対象は lib/ の純粋関数だけで DOM に触らないので
// environment は node。React コンポーネントのテストは入れない。
//
// 拡張子が .mts なのは、package.json に "type": "module" が無いため .ts だと
// CommonJS として読まれ、Vite が「将来の既定（native loader）で壊れる」と警告するため。
import path from "node:path";
import { fileURLToPath } from "node:url";

import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    // tsconfig の paths（"@/*" → "./*"）と同じ解決をテストでも使えるようにする
    alias: { "@": root },
  },
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts"],
  },
});
