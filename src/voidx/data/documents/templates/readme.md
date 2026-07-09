# Document Templates

Use templates by audience first. Human-facing docs optimize for clarity and decision-making; LLM-facing specs optimize for precise implementation and verification.

## Human-Facing Docs

| path | use_when | keywords |
|------|----------|----------|
| `templates/prd.md` | 编写产品需求、功能范围、用户价值、验收标准时读取。保持目的明确、逻辑清晰、简洁可读。 | PRD, product requirements, requirements, user story, acceptance criteria |
| `templates/rfc.md` | 编写方案评审、取舍分析、决策记录或征求意见稿时读取。重点服务人类评审和决策。 | RFC, proposal, decision, trade-off |
| `templates/api-doc.md` | 编写接口、请求响应、错误码和示例调用文档时读取。面向集成方快速理解和使用。 | API, endpoint, request, response, schema |
| `templates/readme.md` | 编写项目说明、安装步骤、使用方式和开发指南时读取。面向首次接触项目的人。 | README, quickstart, install, usage |

## Mixed Human + LLM Docs

| path | use_when | keywords |
|------|----------|----------|
| `templates/tech-design.md` | 编写技术设计时读取。前半部分给人评审方案，后半部分给 LLM 明确实现约束、边界和测试计划。 | technical design, architecture, implementation notes, test plan |

## LLM-Facing Specs

| path | use_when | keywords |
|------|----------|----------|
| `templates/implementation-spec.md` | 已有人类批准的 PRD/RFC/design 后，把需求转成 LLM 可执行工程规格时读取。 | implementation spec, engineering constraints, invariants, forbidden changes |
| `templates/tasks.md` | 把实现规格拆成 TDD 小任务、测试命令和完成标准时读取。 | tasks, TDD, implementation plan, verification |
| `templates/capability-spec.md` | 记录系统能力的新建、修改、兼容性和可验收场景时读取。适合长期维护和防止行为漂移。 | capability, requirement, scenario, compatibility, traceability |
