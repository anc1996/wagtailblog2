"""
Wagtail Page 正文外置通用协议 - Page Mixin 定义。

页面模型通过继承此类并声明模型级 content_storage = {"body": "mongo"} 显式接入外置正文通用协议。
未继承本 Mixin 的普通页面（如 HomePage、PortfolioPage）严格保持原生存储行为。
"""

from typing import Any, ClassVar, Dict, Optional


class ExternalContentPageMixin:
    """
    Wagtail Page 正文外置通用能力混入基类。

    规范约束：
    1. 页面必须显式继承本 Mixin，才会被系统启动检查与生命周期适配器纳入外置流程；
    2. 必须声明模型级属性 content_storage 字典，指定外置字段及其存储后端；
    3. 可选声明 content_storage_schema 版本号（默认为 1，必须为正整数）；
    4. 普通序列化（如导出、比对）纯只读并保留原生值，绝不触发 MongoDB 写入副作用；
    5. 在 G2 完整生命周期协调器（prepare_revision / 原子双写 / Manifest 注入）接线完成前，
       显式阻断外置页面的裸 save_revision 保存，防止生成缺少 Manifest 的空正文损坏 Revision。
    """

    content_storage: ClassVar[Optional[Dict[str, str]]] = None
    content_storage_schema: ClassVar[int] = 1

    def serializable_data(self) -> Dict[str, Any]:
        """
        获取序列化字典。

        严格契约：
        1. 普通序列化纯只读，完整保留原生值，绝不触发 MongoDB 写入副作用；
        2. 占位值与完整 Manifest 快照必须在受控保存上下文中配对生成，
           禁止在无 Manifest 的情况下保存空哨兵，防止生成不可恢复的损坏 Revision。
        """
        return super().serializable_data()  # type: ignore[misc]

    def save_revision(self, *args: Any, **kwargs: Any) -> Any:
        """
        Wagtail Revision 保存入口拦截。

        在 G2 完整生命周期协调器（prepare_revision / 原子双写 / Manifest 注入）
        接线完成前，显式阻断外置页面的裸 save_revision 保存，防止保存无 Manifest 的 Revision。
        """
        raise NotImplementedError(
            "外置正文页面的 save_revision 需由 G2 生命周期协调器受控执行，当前 G1 协议内核阶段禁止直接调用原生 save_revision 以免生成缺失 Manifest 的 Revision"
        )
