"""Local account configuration. Credentials never enter project state."""
from dataclasses import dataclass, field
from datetime import date
import math
import os
from urllib.parse import urlsplit

from . import credential_store

ENV_NAMES = (
    'QWEN_ASR_ENDPOINT', 'DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY',
    'QWEN_ASR_INPUT_CNY_PER_MILLION', 'QWEN_ASR_OUTPUT_CNY_PER_MILLION',
    'CLOUD_PRICING_VERIFIED_ON',
    'CLOUD_PRICING_REFERENCE', 'DEEPSEEK_INPUT_CNY_PER_MILLION',
    'DEEPSEEK_OUTPUT_CNY_PER_MILLION',
)
SILICONFLOW_ENV_NAMES = ('SILICONFLOW_API_KEY', 'SILICONFLOW_ASR_CNY_PER_SECOND',
                        'SILICONFLOW_PRICING_VERIFIED_ON', 'SILICONFLOW_PRICING_REFERENCE')


class ConfigurationRequired(ValueError):
    pass


def read_environment(names):
    """Read allowed legacy configuration, then prefer locally encrypted keys."""
    allowed = frozenset((*ENV_NAMES, *SILICONFLOW_ENV_NAMES))
    names = tuple(name for name in names if name in allowed)
    values = {name: os.environ.get(name, '') for name in names}
    if os.name == 'nt':
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
                for name in names:
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        if isinstance(value, str) and value.strip():
                            values[name] = value
                    except OSError:
                        pass
        except OSError:
            pass
    try:
        saved = credential_store.read_saved_secrets()
    except credential_store.CredentialStoreError as error:
        raise ConfigurationRequired(str(error)) from None
    for name in names:
        if name in credential_store.SUPPORTED_KEYS and name in saved:
            values[name] = saved[name]
    return values


def environment():
    return read_environment(ENV_NAMES)


@dataclass(frozen=True)
class SiliconFlowSettings:
    key: str = field(repr=False)
    price_per_second: float
    verified_on: str
    pricing_reference: str

    def public_config(self):
        return {'price_per_second': self.price_per_second, 'verified_on': self.verified_on,
                'pricing_reference': self.pricing_reference}


def load_siliconflow_settings(values=None, *, today=None):
    values = read_environment(SILICONFLOW_ENV_NAMES) if values is None else values
    key = values.get('SILICONFLOW_API_KEY', '')
    if not isinstance(key, str) or not key.strip() or any(char.isspace() or ord(char)<32 or ord(char)==127 for char in key):
        raise ConfigurationRequired('请先在本机配置有效的硅基流动 API Key')
    raw_rate = values.get('SILICONFLOW_ASR_CNY_PER_SECOND')
    try:
        if isinstance(raw_rate, bool):raise ValueError
        rate = float(raw_rate)
        if not math.isfinite(rate) or rate < 0:raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ConfigurationRequired('请填写已核实的硅基流动每秒人民币价格（非负有限数值）') from None
    try:
        verified = date.fromisoformat(str(values.get('SILICONFLOW_PRICING_VERIFIED_ON', '')).strip())
    except ValueError:
        raise ConfigurationRequired('请核实硅基流动当前价格，核价日期应为 YYYY-MM-DD') from None
    if not 0 <= ((today or date.today()) - verified).days <= 7:
        raise ConfigurationRequired('硅基流动价格核实已超过7天或日期在未来，请重新核价')
    reference = values.get('SILICONFLOW_PRICING_REFERENCE', '')
    if not isinstance(reference, str) or not reference.strip() or len(reference.strip()) > 500:
        raise ConfigurationRequired('请填写硅基流动当前账户报价来源（不超过500字）')
    return SiliconFlowSettings(key.strip(), rate, verified.isoformat(), reference.strip())


@dataclass(frozen=True)
class CloudSettings:
    asr_endpoint: str
    asr_key: str = field(repr=False)
    deepseek_key: str = field(repr=False)
    asr_input_rate: float
    asr_output_rate: float
    deepseek_input_rate: float
    deepseek_output_rate: float
    verified_on: str
    pricing_reference: str

    def public_config(self):
        return {
            'asr_endpoint': self.asr_endpoint,
            'asr_input_rate': self.asr_input_rate,
            'asr_output_rate': self.asr_output_rate,
            'deepseek_input_rate': self.deepseek_input_rate,
            'deepseek_output_rate': self.deepseek_output_rate,
            'verified_on': self.verified_on,
            'pricing_reference': self.pricing_reference,
        }


def load_settings(environ=None, *, require_deepseek=True, today=None):
    values = environment() if environ is None else environ
    missing = []
    for name in ('DASHSCOPE_API_KEY','CLOUD_PRICING_VERIFIED_ON','CLOUD_PRICING_REFERENCE'):
        if not str(values.get(name, '')).strip():
            missing.append(name)
    if require_deepseek and not str(values.get('DEEPSEEK_API_KEY','')).strip():
        missing.append('DEEPSEEK_API_KEY')
    if missing:
        raise ConfigurationRequired('尚未配置：' + '、'.join(missing))
    endpoint = str(values.get('QWEN_ASR_ENDPOINT') or 'https://dashscope.aliyuncs.com/api/v1').strip().rstrip('/')
    parts = urlsplit(endpoint)
    import re
    if (parts.scheme != 'https' or not parts.hostname or not (
        parts.hostname in ('dashscope.aliyuncs.com','maas.qianwenaiapi.com') or
        re.fullmatch(r'[a-zA-Z0-9-]+\.cn-beijing\.maas\.aliyuncs\.com',parts.hostname)) or
        parts.username or parts.password or parts.query or parts.fragment or
        parts.path != '/api/v1' or parts.port not in (None,443)):
        raise ConfigurationRequired('识别地址必须是百炼北京或千问AI平台官方 HTTPS 地址，路径为 /api/v1')
    def number(name, default=None):
        try:
            value=float(values.get(name) or default)
        except (TypeError,ValueError):
            raise ConfigurationRequired(name+' 必须填写已核实的正数价格') from None
        if not math.isfinite(value) or value <= 0:
            raise ConfigurationRequired(name+' 必须填写已核实的正数价格')
        return value
    try:
        verified=date.fromisoformat(str(values['CLOUD_PRICING_VERIFIED_ON']).strip())
    except ValueError:
        raise ConfigurationRequired('核价日期应为 YYYY-MM-DD') from None
    age=((today or date.today())-verified).days
    if not 0 <= age <= 7:
        raise ConfigurationRequired('价格核实已超过7天或日期在未来，请重新核价')
    reference=str(values['CLOUD_PRICING_REFERENCE']).strip()
    if len(reference)>500:
        raise ConfigurationRequired('报价来源说明过长')
    return CloudSettings(endpoint, str(values['DASHSCOPE_API_KEY']).strip(),
        str(values.get('DEEPSEEK_API_KEY','')).strip(),
        number('QWEN_ASR_INPUT_CNY_PER_MILLION',.8),
        number('QWEN_ASR_OUTPUT_CNY_PER_MILLION',2.7),
        number('DEEPSEEK_INPUT_CNY_PER_MILLION',2),
        number('DEEPSEEK_OUTPUT_CNY_PER_MILLION',8),
        verified.isoformat(),reference)


def readiness():
    report={'credentials':{'DASHSCOPE_API_KEY':False,'DEEPSEEK_API_KEY':False}}
    try:
        values=environment()
        report['credentials']={name:bool(values.get(name)) for name in report['credentials']}
        settings=load_settings(values)
        report.update(ready=True,pricing=settings.public_config(),message='配置齐全，尚未验证真实服务连接')
    except (ConfigurationRequired,ValueError) as error:
        report.update(ready=False,message=str(error))
    return report
