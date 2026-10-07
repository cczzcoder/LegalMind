import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 开发代理目标。
 *
 * 容器内（compose）后端服务名是 `api`；**在宿主机上跑 `npm run dev` 时 `api` 解析不到**，
 * 用 `VITE_API_TARGET=http://127.0.0.1:8000 npm run dev` 覆盖。
 *
 * 这里不直接写 `process.env`：项目没有装 `@types/node`，配置里 `process` 无类型；
 * 从 `globalThis` 取一份带类型的视图，避免**为一个构建配置引入整包类型**。
 */
const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;
const apiTarget = env?.VITE_API_TARGET ?? "http://api:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": apiTarget,
      "/health": apiTarget,
    },
  },
  build: {
    rollupOptions: {
      output: {
        // 把「体积大且很少变」的依赖拆成独立 chunk：改业务代码时用户不必重下 antd。
        // 页面本身已按路由懒加载（见 App.tsx），所以这里只处理 vendor。
        manualChunks: {
          react: ["react", "react-dom"],
          antd: ["antd", "@ant-design/icons"],
        },
      },
    },
  },
});
