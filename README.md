# Personal Assistant Agent｜个人智能事务助手

**基于 LangGraph \+ FastAPI \+ Vue 3 构建的多用户智能事务 Agent**

内置长期记忆、智能上下文压缩、工具调用幂等、危险操作二次确认、调用审计日志等企业级能力，可用于个人日程、邮件、知识库、提醒自动化管理。

**✨ 项目亮点**

* **多用户隔离**：用户、会话、记忆、审计日志完全隔离，支持多用户使用

* **智能上下文管理**：三层 Token 压缩策略，彻底解决超长对话溢出问题

* **长期用户记忆**：自动记录用户偏好，跨会话持久生效

* **安全可靠执行**：工具风险分级、二次确认、自动重试、幂等防重、完整审计

* **模块化可扩展**：新增工具、对接第三方 API 无需改动核心流程

* **开箱即用**：前后端完整，一条命令本地启动

**🚀 技术栈**

* 后端：FastAPI、SQLAlchemy\(异步\)、LangChain、LangGraph

* 前端：Vue 3（Vite + TypeScript + Vue Router）

* 数据：SQLite（可无缝替换 PostgreSQL）

* AI：OpenAI / 兼容大模型接口

**📌 核心能力**

* 多轮对话 \+ 会话管理，历史记录持久化

* 日程查询/创建、邮件检索/发送、Notion 页面创建、定时提醒

* AI 自动总结超长对话、自动精简工具返回数据

* 危险操作人工确认、超时自动作废

* 工具调用幂等执行，避免重复创建、重复发送

* 全流程操作审计日志，可追溯

**📁 快速启动**

    # 安装依赖
    pip install -r requirements.txt

    # 启动后端
    uvicorn backend.main:app --reload --port 8000

    # 启动前端
    cd frontend
    npm install
    npm run dev

    # 前端地址：http://localhost:5173
    # 开发服务器会把 /api 请求代理到 http://localhost:8000，无需处理跨域

    # 生产构建（产物在 frontend/dist）
    npm run build
    npm run preview

**🎯 适用场景**

个人智能秘书、AI 自动化事务处理、大模型 Agent 学习项目、可商用私有化轻量智能助手底座。

**📎 扩展方向**

可无缝对接真实 Notion / 谷歌日历 / 企业邮箱 API、接入多模态、增加流式输出、分布式部署、权限系统与限流监控。

