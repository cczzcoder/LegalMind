import { Result } from "antd";
import { Link } from "../components/Link";

/** 404：hash 里出现未知路径时。给出回首页的路，不让人卡住。 */
export default function NotFoundPage() {
  return (
    <Result
      status="404"
      title="页面不存在"
      subTitle="地址可能拼错了，或者这个页面还没做。"
      extra={<Link to="/ask">回到问答</Link>}
    />
  );
}
