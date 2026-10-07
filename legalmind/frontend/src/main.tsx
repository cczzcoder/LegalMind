import { App as AntdApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import dayjs from "dayjs";
import "dayjs/locale/zh-cn";
import React from "react";
import ReactDOM from "react-dom/client";

import App from "./App";
import { SessionProvider } from "./app/session";
import { theme } from "./app/theme";
import "./style.css";

// 中文日期格式（`dayjs` 是 antd 的传递依赖，不新增）
dayjs.locale("zh-cn");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} theme={theme}>
      {/* antd 的 App 提供 message/notification 的上下文（与本站的 App 组件同名，故用别名） */}
      <AntdApp>
        <SessionProvider>
          <App />
        </SessionProvider>
      </AntdApp>
    </ConfigProvider>
  </React.StrictMode>,
);
