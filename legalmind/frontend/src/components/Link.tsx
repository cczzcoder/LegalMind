import type { AnchorHTMLAttributes, ReactNode } from "react";

/**
 * 站内链接。**用原生 `<a href="#/…">`**：hash 路由下浏览器自己就会跳转并派发 `hashchange`，
 * 不需要拦截点击——这样中键、右键「新标签打开」、键盘回车都是免费的。
 */
export function Link({
  to,
  children,
  ...rest
}: { to: string; children: ReactNode } & Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href">) {
  return (
    <a href={`#${to}`} {...rest}>
      {children}
    </a>
  );
}
