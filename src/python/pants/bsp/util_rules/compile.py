# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Type, TypeVar

from pants.bsp.context import BSPContext
from pants.bsp.protocol import BSPHandlerMapping
from pants.bsp.spec.base import BuildTargetIdentifier, StatusCode, TaskId
from pants.bsp.spec.compile import CompileParams, CompileReport, CompileResult, CompileTask
from pants.bsp.spec.task import TaskFinishParams, TaskStartParams
from pants.bsp.util_rules.targets import BSPBuildTargetInternal, BSPCompileRequest, BSPCompileResult
from pants.engine.fs import Workspace
from pants.engine.internals.native_engine import EMPTY_DIGEST, Digest, MergeDigests
from pants.engine.internals.selectors import Get, MultiGet
from pants.engine.rules import _uncacheable_rule, collect_rules
from pants.engine.target import FieldSet, Targets
from pants.engine.unions import UnionMembership, UnionRule
from pants.util.ordered_set import FrozenOrderedSet

_logger = logging.getLogger(__name__)

_FS = TypeVar("_FS", bound=FieldSet)


class CompileRequestHandlerMapping(BSPHandlerMapping):
    method_name = "buildTarget/compile"
    request_type = CompileParams
    response_type = CompileResult


@dataclass(frozen=True)
class CompileOneBSPTargetRequest:
    bsp_target: BSPBuildTargetInternal

    # A unique identifier generated by the client to identify this request.
    # The server may include this id in triggered notifications or responses.
    origin_id: str | None = None

    # Optional arguments to the compilation process.
    arguments: tuple[str, ...] | None = ()


@_uncacheable_rule
async def compile_bsp_target(
    request: CompileOneBSPTargetRequest,
    bsp_context: BSPContext,
    union_membership: UnionMembership,
) -> BSPCompileResult:
    targets = await Get(Targets, BSPBuildTargetInternal, request.bsp_target)
    compile_request_types: FrozenOrderedSet[Type[BSPCompileRequest]] = union_membership.get(
        BSPCompileRequest
    )
    field_sets_by_request_type: dict[Type[BSPCompileRequest], set[FieldSet]] = defaultdict(set)
    for target in targets:
        for compile_request_type in compile_request_types:
            field_set_type = compile_request_type.field_set_type
            if field_set_type.is_applicable(target):
                field_set = field_set_type.create(target)
                field_sets_by_request_type[compile_request_type].add(field_set)

    task_id = TaskId(id=uuid.uuid4().hex)

    bsp_context.notify_client(
        TaskStartParams(
            task_id=task_id,
            event_time=int(time.time() * 1000),
            data=CompileTask(target=request.bsp_target.bsp_target_id),
        )
    )

    compile_results = await MultiGet(
        Get(
            BSPCompileResult,
            BSPCompileRequest,
            compile_request_type(bsp_target=request.bsp_target, field_sets=tuple(field_sets)),
        )
        for compile_request_type, field_sets in field_sets_by_request_type.items()
    )

    status = StatusCode.OK
    if any(r.status != StatusCode.OK for r in compile_results):
        status = StatusCode.ERROR

    bsp_context.notify_client(
        TaskFinishParams(
            task_id=task_id,
            event_time=int(time.time() * 1000),
            status=status,
            data=CompileReport(
                target=request.bsp_target.bsp_target_id,
                origin_id=request.origin_id,
                errors=0,
                warnings=0,
            ),
        )
    )

    output_digest = await Get(Digest, MergeDigests([r.output_digest for r in compile_results]))

    return BSPCompileResult(
        status=status,
        output_digest=output_digest,
    )


@_uncacheable_rule
async def bsp_compile_request(
    request: CompileParams,
    workspace: Workspace,
) -> CompileResult:
    bsp_targets = await MultiGet(
        Get(BSPBuildTargetInternal, BuildTargetIdentifier, bsp_target_id)
        for bsp_target_id in request.targets
    )

    compile_results = await MultiGet(
        Get(
            BSPCompileResult,
            CompileOneBSPTargetRequest(
                bsp_target=bsp_target,
                origin_id=request.origin_id,
                arguments=request.arguments,
            ),
        )
        for bsp_target in bsp_targets
    )

    output_digest = await Get(Digest, MergeDigests([r.output_digest for r in compile_results]))
    if output_digest != EMPTY_DIGEST:
        workspace.write_digest(output_digest, path_prefix=".pants.d/bsp")

    status_code = StatusCode.OK
    if any(r.status != StatusCode.OK for r in compile_results):
        status_code = StatusCode.ERROR

    return CompileResult(
        origin_id=request.origin_id,
        status_code=status_code.value,
    )


def rules():
    return (
        *collect_rules(),
        UnionRule(BSPHandlerMapping, CompileRequestHandlerMapping),
    )
