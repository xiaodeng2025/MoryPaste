"""Resolve data storage once, with fail-closed portable test profiles."""
import json
import os
import sys
from pathlib import Path


def resolve_data_dir(env=None, resource_dir=None):
    env = os.environ if env is None else env
    resource_dir = Path(resource_dir or getattr(sys, '_MEIPASS', Path(__file__).parent))
    main = (Path(env.get('LOCALAPPDATA', str(Path.home()))) / 'SendToCopyQ').resolve()
    configured = env.get('SENDTOCOPYQ_DATA_DIR', '').strip()
    marker = resource_dir / 'test-data-profile.json'
    if marker.exists():
        profile = json.loads(marker.read_text('utf-8'))
        expected = (main.parent / profile['directory']).resolve()
        if profile['directory'] != 'SendToCopyQ-PairingTest' or not configured:
            raise RuntimeError('测试版缺少隔离配置。请使用 Start-SendToCopyQ-Test.bat 启动。')
        selected = Path(configured)
        if not selected.is_absolute() or selected.resolve() != expected or selected.resolve() == main:
            raise RuntimeError('测试数据目录不正确，已阻止访问主线数据。')
        return selected.resolve()
    return Path(configured).resolve() if configured else main


def child_environment(data_dir):
    env = os.environ.copy()
    env['SENDTOCOPYQ_DATA_DIR'] = str(Path(data_dir).resolve())
    env['PYTHONUNBUFFERED'] = '1'
    return env
