"""mars 相关接口(群消息同步 / 任务拉取)的请求模型。"""

from pydantic import BaseModel, Field


class SyncRunRequest(BaseModel):
    dry_run: bool = Field(False, description="只看会推什么,不真推(不动水位)")
    limit: int | None = Field(
        None, ge=1, le=2000, description="本轮最多处理多少条,默认取配置的 batch"
    )


class WatermarkRequest(BaseModel):
    """重设水位 —— 三选一,都不给就贴到当前最新一条(= 跳过所有积压)。"""

    send_time: int | None = Field(None, ge=0, description="unix 秒;精确定位用")
    lid: int | None = Field(None, ge=0, description="同秒内的次级序,配合 send_time")
    backfill_seconds: int | None = Field(
        None, ge=0, le=30 * 24 * 3600, description="往回补多少秒的历史(会重放下游副作用)"
    )
    rid: int | None = Field(None, ge=0, description="只重设这个群;不填表示所有活动群")
    resume_from_tombstone: bool = Field(
        False, description="从群重新加入前保存的旧水位追赶;必须同时指定 rid"
    )


class TaskRunRequest(BaseModel):
    """手动跑一轮任务拉取。"""

    wait: float = Field(
        0.0, ge=0, le=50, description="长轮询等多少秒。手动验证时留 0:队列空就立刻返回"
    )
