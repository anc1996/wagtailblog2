"""BlogPage 发布编排的最小兼容服务。

本模块负责锁定 MySQL 页面/Revision、验证 Mongo 不可变正文版本，并为
Wagtail Workflow 与定时发布提供精确 Revision 围栏。搜索 Outbox 和正式正文
指针切换仍由后续批次接入。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

from django.db import transaction
from django.utils import timezone
from wagtailblog3.mongo import MongoRevisionReadError


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
	from wagtail.models import Revision, WorkflowState
	from blog.models import BlogPage, BlogPublicationState


class PublicationRevisionInvalidError(RuntimeError):
	"""指定 Revision 缺少可发布正文版本元数据，或元数据类型不正确。"""


class PublicationBodyUnavailableError(RuntimeError):
	"""指定 Revision 的 Mongo 正文版本不可读取或校验失败。"""


@dataclass(frozen=True)
class BlogPublicationCandidate:
	"""已加锁并完成正文校验的发布候选。

	``state`` 和 ``revision`` 只在调用方仍处于外层事务时具备锁语义；
	本批不执行发布，因此返回值仅供后续发布服务继续完成一致性校验。
	"""

	page: BlogPage
	revision: Revision
	state: BlogPublicationState
	body_document: Mapping[str, Any]


class PublicationWorkflowRevisionDriftError(RuntimeError):
	"""Workflow 批准后页面产生新 Revision 时阻止隐式发布新正文。"""


def validate_scheduled_revision(revision: Revision) -> None:
	"""校验 Wagtail 定时发布任务传入的已批准 Revision。

	Wagtail 的定时命令按 ``approved_go_live_at`` 查询 Revision，再调用
	``Revision.publish``；期间页面可以产生更新的草稿，但不能因此改发最新草稿。
	这里只验证调度标记和到期时间，正文身份仍由发布候选服务锁定并校验。
	"""
	approved_at = getattr(revision, "approved_go_live_at", None)
	if approved_at is None:
		raise PublicationRevisionInvalidError("scheduled_revision_not_approved")
	if approved_at > timezone.now():
		raise PublicationRevisionInvalidError("scheduled_revision_not_due")


def _revision_content(revision: Revision) -> Mapping[str, Any]:
	"""将 Wagtail Revision.content 统一解析为 JSON 映射。"""

	content: Any = revision.content
	if isinstance(content, str):
		try:
			content = json.loads(content)
		except (TypeError, ValueError) as exc:
			raise PublicationRevisionInvalidError("revision_content_invalid") from exc
	if not isinstance(content, Mapping):
		raise PublicationRevisionInvalidError("revision_content_invalid")
	return content


def _version_metadata(content: Mapping[str, Any]) -> tuple[str, str, int]:
	"""提取并校验不可变正文版本三元组。"""

	version_id = content.get("mongo_body_version_id")
	body_sha256 = content.get("body_sha256")
	schema_version = content.get("body_schema_version")
	if (
		not isinstance(version_id, str)
		or not version_id
		or not isinstance(body_sha256, str)
		or len(body_sha256) != 64
		or not isinstance(schema_version, int)
		or isinstance(schema_version, bool)
		or schema_version < 1
	):
		raise PublicationRevisionInvalidError("revision_body_version_metadata_invalid")
	return version_id, body_sha256, schema_version


class BlogPublicationService:
	"""锁定页面并准备一个经过 Mongo 正文校验的发布候选。"""

	@classmethod
	def lock_and_validate_revision(
		cls,
		page_id: int,
		revision_id: int,
		*,
		approved_revision_id: int | None = None,
	) -> BlogPublicationCandidate:
		"""在 MySQL 事务内锁定页面和 Revision，并校验其 Mongo 正文版本。

		参数：``page_id`` 为 BlogPage 主键，``revision_id`` 为指定 Revision；
		``approved_revision_id`` 仅记录审批元数据，不触发 Workflow。
		返回：带有已锁定页面、Revision、状态和 Mongo 正文文档的候选对象。
		异常：元数据缺失抛 ``PublicationRevisionInvalidError``；Mongo 读取/校验
		失败抛 ``PublicationBodyUnavailableError``。Mongo 校验失败时状态表不会保存。
		副作用：成功时仅更新/创建 ``BlogPublicationState`` 的草稿指针字段。
		"""

		if not isinstance(page_id, int) or isinstance(page_id, bool) or page_id < 1:
			raise ValueError("page_id_invalid")
		if not isinstance(revision_id, int) or isinstance(revision_id, bool) or revision_id < 1:
			raise ValueError("revision_id_invalid")

		with transaction.atomic():
			from wagtail.models import Revision
			from blog.models import BlogPage, BlogPublicationState

			page = BlogPage.objects.select_for_update().get(pk=page_id)
			state, _ = BlogPublicationState.objects.select_for_update().get_or_create(
				page_id=page.pk,
				defaults={"publication_generation": 0},
			)
			revision = Revision.objects.select_for_update().get(
				pk=revision_id,
				object_id=str(page.pk),
			)
			content = _revision_content(revision)
			version_id, body_sha256, schema_version = _version_metadata(content)
			try:
				# 沿用 blog.models 的 MongoManager 注入边界，保证发布校验与页面保存使用同一适配器。
				from blog import models as blog_models

				body_document = blog_models.MongoManager().get_content_body_version(
					"blog_page",
					page.pk,
					version_id,
					body_sha256,
					schema_version,
				)
			except MongoRevisionReadError as exc:
				raise PublicationBodyUnavailableError("mongo_body_version_unavailable") from exc
			if not isinstance(body_document, Mapping) or not isinstance(body_document.get("body"), list):
				raise PublicationBodyUnavailableError("mongo_body_version_invalid")

			state.draft_body_version_id = version_id
			state.draft_body_sha256 = body_sha256
			state.draft_body_schema_version = schema_version
			logger.info(
				"blog_publication_candidate_validated page_id=%s revision_id=%s body_version_id=%s approved_revision_id=%s",
				page.pk,
				revision.pk,
				version_id,
				approved_revision_id,
			)
			if approved_revision_id is not None:
				if not isinstance(approved_revision_id, int) or isinstance(approved_revision_id, bool):
					raise ValueError("approved_revision_id_invalid")
				state.approved_revision_id = approved_revision_id
				state.approved_revision_created_at = getattr(revision, "created_at", None)
				state.approved_body_version_id = version_id
				state.approved_body_sha256 = body_sha256
				state.approved_body_schema_version = schema_version
			state.save(
				update_fields=[
					"draft_body_version_id",
					"draft_body_sha256",
					"draft_body_schema_version",
					"approved_revision_id",
					"approved_revision_created_at",
					"approved_body_version_id",
					"approved_body_sha256",
					"approved_body_schema_version",
					"updated_at",
				]
			)
			return BlogPublicationCandidate(page, revision, state, body_document)

	@classmethod
	def promote_published_candidate(cls, candidate: BlogPublicationCandidate) -> BlogPublicationState:
		"""在 Wagtail 发布动作发出信号前切换正式正文指针并递增公开代次。

		该方法必须由外层 MySQL 事务调用；搜索信号随后读取同一行，因而 Outbox
		快照与页面 ``live_revision`` 更新要么一起提交，要么一起回滚。
		"""
		state = candidate.state
		content = _revision_content(candidate.revision)
		if content is None:
			raise PublicationRevisionInvalidError("revision_content_invalid")
		version_id, body_sha256, schema_version = _version_metadata(content)
		state.published_body_version_id = version_id
		state.published_body_sha256 = body_sha256
		state.published_body_schema_version = schema_version
		state.publication_generation = (state.publication_generation or 0) + 1
		state.save(
			update_fields=(
				"published_body_version_id",
				"published_body_sha256",
				"published_body_schema_version",
				"publication_generation",
				"updated_at",
			)
		)
		logger.info(
			"blog_publication_state_promoted page_id=%s revision_id=%s body_version_id=%s generation=%s",
			candidate.page.pk,
			candidate.revision.pk,
			version_id,
			state.publication_generation,
		)
		return state

	@classmethod
	def ensure_published_revision(cls, page_id: int, revision_id: int) -> BlogPublicationState:
		"""在 Wagtail 已完成页面保存后补齐指定 Revision 的正式指针。

		Wagtail 8.0 的编辑发布动作直接调用 ``PublishPageRevisionAction``，不会经过
		``BlogPage.publish``。该入口由 ``page_published`` 信号调用，因此必须保持幂等：
		已有相同正文版本时不重复增加代次；版本缺失或发生变化时才重新校验并切换指针。
		副作用：在当前 MySQL 事务中更新/创建 ``BlogPublicationState``；Mongo 正文缺失
		或 Revision 身份不匹配时抛出发布校验异常，不写入正式指针。
		"""
		with transaction.atomic():
			candidate = cls.lock_and_validate_revision(page_id, revision_id)
			content = _revision_content(candidate.revision)
			version_id, body_sha256, schema_version = _version_metadata(content)
			state = candidate.state
			if (
				state.published_body_version_id == version_id
				and state.published_body_sha256 == body_sha256
				and state.published_body_schema_version == schema_version
			):
				return state
			return cls.promote_published_candidate(candidate)

	@classmethod
	def advance_unpublish_generation(cls, page_id: int) -> BlogPublicationState:
		"""为取消发布生成新的公开代次，同时暂留旧指针供 tombstone 使用。"""
		from blog.models import BlogPublicationState

		state, _ = BlogPublicationState.objects.select_for_update().get_or_create(
			page_id=page_id,
			defaults={"publication_generation": 0},
		)
		state.publication_generation = (state.publication_generation or 0) + 1
		state.save(update_fields=("publication_generation", "updated_at"))
		logger.info(
			"blog_publication_generation_advanced page_id=%s generation=%s",
			page_id,
			state.publication_generation,
		)
		return state

	@classmethod
	def clear_published_pointer(cls, page_id: int) -> None:
		"""在取消发布 tombstone 已写入当前事务后清空正式正文指针。"""
		from blog.models import BlogPublicationState

		BlogPublicationState.objects.filter(page_id=page_id).update(
			published_body_version_id=None,
			published_body_sha256=None,
			published_body_schema_version=None,
		)
		logger.info("blog_publication_pointer_cleared page_id=%s", page_id)


def _workflow_approved_revision(workflow_state: WorkflowState) -> Revision:
	"""返回 Workflow 最终成功任务绑定的 Revision，避免使用页面最新草稿。"""
	from wagtail.models import TaskState

	current = getattr(workflow_state, "current_task_state", None)
	if current and current.status in (TaskState.STATUS_APPROVED, TaskState.STATUS_SKIPPED):
		return current.revision
	candidate = (
		workflow_state.task_states.filter(
			status__in=(TaskState.STATUS_APPROVED, TaskState.STATUS_SKIPPED),
			revision__isnull=False,
		)
		.select_related("revision")
		.order_by("-finished_at", "-id")
		.first()
	)
	if not candidate:
		raise PublicationWorkflowRevisionDriftError("workflow_approved_revision_missing")
	return candidate.revision


def finish_workflow_action(workflow_state: WorkflowState, user=None) -> None:
	"""Wagtail Workflow 完成时冻结并发布准确的 BlogPage 审批 Revision。

	Wagtail 默认动作读取页面最新 Revision；审批完成后若有人保存了新草稿，
	该行为会绕过审核。此动作在同一事务中锁定状态并拒绝 Revision 漂移。
	"""
	with transaction.atomic():
		from blog.models import BlogPage
		from wagtail.models import WorkflowState

		locked_state = WorkflowState.objects.select_for_update().get(pk=workflow_state.pk)
		content_object = locked_state.content_object
		page = getattr(content_object, "specific", content_object)
		if not isinstance(page, BlogPage):
			from wagtail.workflows import publish_workflow_state

			publish_workflow_state(locked_state, user=user)
			return
		approved_revision = _workflow_approved_revision(locked_state)
		latest_revision = page.get_latest_revision()
		if not latest_revision or latest_revision.pk != approved_revision.pk:
			raise PublicationWorkflowRevisionDriftError("workflow_revision_drift")
		BlogPublicationService.lock_and_validate_revision(
			page.pk,
			approved_revision.pk,
			approved_revision_id=approved_revision.pk,
		)
		approved_revision.publish(user=user)
