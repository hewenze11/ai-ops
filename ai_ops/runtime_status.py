"""Safe runtime capabilities: never return provider credentials or secret paths."""
import os
from fastapi import Depends


def model_status(app):
    threads = getattr(app.state, 'model_worker_threads', [])
    legacy_provider = bool(os.environ.get('AI_OPS_MODEL_BASE_URL') and os.environ.get('AI_OPS_MODEL_KEY_FILE'))
    return {'worker_running': bool(threads and any(t.is_alive() for t in threads)),
            'provider_configured': legacy_provider,
            'default_model': app.state.role_engine.default_model,
            'provider_scope': 'role_or_global' if legacy_provider else 'role',
            'configuration_help': '每个新角色需独立配置 OpenAI 兼容 HTTPS 接口、模型和 API Key。密钥只写入服务端受控文件，读取只返回已配状态；编辑时留空仅保留该角色的 Key，不借用其他角色的配置。'}


def install(app, admin):
    @app.get('/api/v1/runtime/model', dependencies=[Depends(admin)])
    def model():
        return model_status(app)

    @app.get('/api/v1/runtime/notifications', dependencies=[Depends(admin)])
    def notifications():
        thread = getattr(app.state, 'notification_worker_thread', None)
        return {'worker_running': bool(thread and thread.is_alive()),
                'notice': '后台通知线程关闭时，任务结果不会自动投递；测试按钮仅发送一条即时测试消息，不启用后台线程、不补发历史队列。'}
