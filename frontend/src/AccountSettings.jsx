import { useEffect, useRef, useState } from "react";
import {
  AudioLines,
  BrainCircuit,
  Cloud,
  KeyRound,
  LockKeyhole,
  Save,
  ShieldCheck,
} from "lucide-react";
import { Badge, Button, Notice, SectionHeading } from "./ui";

const providers = [
  {
    id: "siliconflow",
    name: "硅基流动",
    subtitle: "SILICONFLOW",
    purpose: "Qwen3 ASR 样片试听",
    icon: AudioLines,
    color: "violet",
  },
  {
    id: "bailian",
    name: "阿里云百炼",
    subtitle: "MODEL STUDIO",
    purpose: "Qwen ASR 音频识别",
    icon: Cloud,
    color: "orange",
  },
  {
    id: "deepseek",
    name: "DeepSeek",
    subtitle: "DEEPSEEK",
    purpose: "多语言翻译与双语字幕",
    icon: BrainCircuit,
    color: "blue",
  },
];

function useSaveFeedback() {
  const [saved, setSaved] = useState(false);
  const revision = useRef(0);
  const clear = () => {
    revision.current += 1;
    setSaved(false);
    return revision.current;
  };
  const confirm = (submittedRevision) => {
    if (submittedRevision !== revision.current) return false;
    setSaved(true);
    return true;
  };
  return { saved, clear, confirm };
}

function SaveStatus({ name, children }) {
  return (
    <p className="form-save-status" role="status" aria-label={name} aria-live="polite">
      {children}
    </p>
  );
}

function CredentialCard({ provider, account = {}, disabled, pending, save }) {
  const [key, setKey] = useState("");
  const feedback = useSaveFeedback();
  const Icon = provider.icon;
  const storageName =
    account.storage === "environment" || account.storage === "env"
      ? "来自环境变量"
      : "已加密保存";
  const submit = async (event) => {
    event.preventDefault();
    if (!key.trim()) return;
    const submittedRevision = feedback.clear();
    const saved = await save(
      `key-${provider.id}`,
      "/api/credentials",
      { provider: provider.id, key: key.trim() },
      `${provider.name} API Key 已保存。`,
    );
    if (saved && feedback.confirm(submittedRevision)) setKey("");
  };
  return (
    <form className="card credential-card" onSubmit={submit} autoComplete="off">
      <div className="provider-top">
        <span className={`provider-icon provider-${provider.color}`}>
          <Icon size={25} strokeWidth={1.6} />
        </span>
        <Badge tone={account.configured ? "success" : "neutral"}>
          {account.configured ? storageName : "尚未配置"}
        </Badge>
      </div>
      <h2>{provider.name}</h2>
      <div className="provider-subtitle">{provider.subtitle}</div>
      <p>{provider.purpose}</p>
      <label htmlFor={`key-${provider.id}`}>{provider.name} API Key</label>
      <div className="key-input">
        <KeyRound size={16} />
        <input
          id={`key-${provider.id}`}
          type="password"
          value={key}
          onChange={(event) => {
            feedback.clear();
            setKey(event.target.value);
          }}
          placeholder={
            account.configured ? "输入新 Key 可替换已保存凭据" : "输入 API Key"
          }
          autoComplete="new-password"
          spellCheck="false"
          disabled={disabled}
        />
      </div>
      <Button
        type="submit"
        className="full-width"
        icon={Save}
        disabled={disabled || !key.trim()}
        busy={pending === `key-${provider.id}`}
        aria-label={`保存${provider.name === "阿里云百炼" ? "百炼" : provider.name} Key`}
      >
        保存 Key
      </Button>
      {feedback.saved && (
        <SaveStatus name={`${provider.name} Key 保存结果`}>
          API Key 已保存；保存配置不会调用模型。
        </SaveStatus>
      )}
      <small>
        <LockKeyhole size={12} />
        保存后不会回显密钥
      </small>
    </form>
  );
}

function SiliconflowPricing({ account, disabled, pending, save }) {
  const [rate, setRate] = useState("0.000220");
  const [reference, setReference] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const feedback = useSaveFeedback();
  const initialized = useRef(false);
  useEffect(() => {
    if (account && !initialized.current) {
      setRate(
        account.price_per_second == null || account.price_per_second === ""
          ? "0.000220"
          : String(account.price_per_second),
      );
      setReference(account.pricing_reference || "");
      initialized.current = true;
    }
  }, [account]);
  const validRate =
    rate.trim() !== "" && Number.isFinite(Number(rate)) && Number(rate) >= 0;
  const estimate = validRate
    ? `¥${(Number(rate) * 300).toLocaleString("en-US", { minimumFractionDigits: 3, maximumFractionDigits: 6 })}`
    : "请输入有效单价";
  const canSave = validRate && reference.trim() && confirmed;
  const submit = async (event) => {
    event.preventDefault();
    if (!canSave) return;
    const submittedRevision = feedback.clear();
    const saved = await save(
      "siliconflow-prices",
      "/api/siliconflow-settings",
      {
        price_per_second: Number(rate),
        pricing_reference: reference.trim(),
        confirmed: true,
      },
      "硅基流动价格配置已保存，尚未调用模型。",
    );
    if (saved && feedback.confirm(submittedRevision)) setConfirmed(false);
  };
  return (
    <form className="card pricing-card siliconflow-pricing" onSubmit={submit}>
      <SectionHeading
        title="硅基流动 · 价格配置"
        extra={
          <div className="settings-badges">
            <Badge tone={account?.ready ? "success" : "warning"}>
              {account?.ready ? "配置齐全，可试听" : "待完成配置"}
            </Badge>
            <Badge tone="primary">预算 ¥20 / 追加停止线 ¥18</Badge>
          </div>
        }
      >
        Qwen/Qwen3-ASR-1.7B 按音频时长计费，价格确认与 Key 保存独立。
      </SectionHeading>
      <div className="pricing-fields">
        <label>
          硅基流动单价（元 / 秒）
          <input
            type="number"
            min="0"
            step="any"
            value={rate}
            onChange={(event) => {
              feedback.clear();
              setRate(event.target.value);
            }}
            disabled={disabled}
            required
          />
        </label>
        <label>
          五分钟样片预计费用
          <output className="date-readout" aria-label="五分钟样片预计费用">
            {estimate}
          </output>
        </label>
        <label>
          最近核价日期
          <output className="date-readout">
            {account?.verified_on || "尚未核价"}
          </output>
        </label>
        <label>
          硅基流动报价来源
          <input
            value={reference}
            onChange={(event) => {
              feedback.clear();
              setReference(event.target.value);
            }}
            placeholder="填写当前账户模型页或截图来源"
            maxLength={500}
            disabled={disabled}
            required
          />
        </label>
      </div>
      <div className="pricing-footer">
        <label className="checkbox-option">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(event) => {
              feedback.clear();
              setConfirmed(event.target.checked);
            }}
            disabled={disabled}
          />
          <span>我已核实当前硅基流动账户的模型价格</span>
        </label>
        <Button
          type="submit"
          icon={Save}
          variant="primary"
          disabled={disabled || !canSave}
          busy={pending === "siliconflow-prices"}
        >
          保存硅基流动价格
        </Button>
      </div>
      {feedback.saved && (
        <SaveStatus name="硅基流动价格保存结果">
          硅基流动价格配置已保存；保存配置不会调用模型。
        </SaveStatus>
      )}
      <p className="form-footnote">
        参考单价为 ¥0.000220 / 秒，五分钟按 300
        秒估算，实际以服务计费为准。保存时记录当天核价，有效期为 7
        天；保存本身不会调用模型。试听会上传音频，只生成未对齐时间轴的识别文字。
      </p>
    </form>
  );
}

export default function AccountSettings({ snapshot, disabled, pending, save }) {
  const [confirmed, setConfirmed] = useState(false);
  const feedback = useSaveFeedback();
  const [prices, setPrices] = useState({
    asr_endpoint: "",
    asr_input_rate: "",
    asr_output_rate: "",
    deepseek_input_rate: "",
    deepseek_output_rate: "",
    verified_on: "",
    pricing_reference: "",
  });
  const initialized = useRef(false);
  useEffect(() => {
    if (snapshot?.settings && !initialized.current) {
      setPrices((previous) => ({ ...previous, ...snapshot.settings }));
      initialized.current = true;
    }
  }, [snapshot?.settings]);
  const update = (event) => {
    feedback.clear();
    setPrices((previous) => ({
      ...previous,
      [event.target.name]: event.target.value,
    }));
  };
  const submitPrices = async (event) => {
    event.preventDefault();
    const { verified_on, ...publicPrices } = prices;
    const submittedRevision = feedback.clear();
    const saved = await save(
      "prices",
      "/api/settings",
      { ...publicPrices, confirmed },
      "价格配置已保存。执行云端任务时会再次检查有效性。",
    );
    if (saved && feedback.confirm(submittedRevision)) setConfirmed(false);
  };
  return (
    <div className="settings-content">
      {snapshot?.settings_error && (
        <Notice tone="warning">{snapshot.settings_error}</Notice>
      )}
      {snapshot?.job?.busy && (
        <Notice>任务运行期间保留当前账户配置，停止后即可修改。</Notice>
      )}
      <div className="security-banner">
        <span>
          <ShieldCheck size={25} strokeWidth={1.6} />
        </span>
        <div>
          <strong>密钥保存在你的电脑上</strong>
          <p>
            使用 Windows 用户加密保护。保存 Key
            不会调用模型，也不需要先确认价格。
          </p>
        </div>
        <Badge tone="success">本机加密</Badge>
      </div>
      <div className="credential-grid">
        {providers.map((provider) => (
          <CredentialCard
            key={provider.id}
            provider={provider}
            account={snapshot?.accounts?.[provider.id]}
            disabled={disabled}
            pending={pending}
            save={save}
          />
        ))}
      </div>
      <SiliconflowPricing
        account={snapshot?.accounts?.siliconflow}
        disabled={disabled}
        pending={pending}
        save={save}
      />
      <form className="card pricing-card" onSubmit={submitPrices}>
        <SectionHeading
          title="完整字幕流程 · 价格配置"
          extra={<Badge tone="primary">预算 ¥20 / 追加停止线 ¥18</Badge>}
        >
          百炼识别与 DeepSeek 翻译按量计费，请按当前账户实际报价填写
        </SectionHeading>
        <div className="pricing-fields">
          <label className="wide-field">
            百炼识别地址
            <input
              name="asr_endpoint"
              value={prices.asr_endpoint}
              onChange={update}
              placeholder="https://dashscope.aliyuncs.com/api/v1"
              disabled={disabled}
              required
              spellCheck="false"
            />
          </label>
          <fieldset>
            <legend>
              百炼 Qwen ASR <span>元 / 百万 Token</span>
            </legend>
            <div className="rate-pair">
              <label>
                输入价格
                <input
                  name="asr_input_rate"
                  type="number"
                  min="0.000001"
                  step="any"
                  value={prices.asr_input_rate}
                  onChange={update}
                  placeholder="当前核实的价格"
                  disabled={disabled}
                  required
                />
              </label>
              <label>
                输出价格
                <input
                  name="asr_output_rate"
                  type="number"
                  min="0.000001"
                  step="any"
                  value={prices.asr_output_rate}
                  onChange={update}
                  placeholder="当前核实的价格"
                  disabled={disabled}
                  required
                />
              </label>
            </div>
          </fieldset>
          <fieldset>
            <legend>
              DeepSeek 翻译 <span>元 / 百万 Token</span>
            </legend>
            <div className="rate-pair">
              <label>
                输入价格
                <input
                  name="deepseek_input_rate"
                  type="number"
                  min="0.000001"
                  step="any"
                  value={prices.deepseek_input_rate}
                  onChange={update}
                  placeholder="当前核实的价格"
                  disabled={disabled}
                  required
                />
              </label>
              <label>
                输出价格
                <input
                  name="deepseek_output_rate"
                  type="number"
                  min="0.000001"
                  step="any"
                  value={prices.deepseek_output_rate}
                  onChange={update}
                  placeholder="当前核实的价格"
                  disabled={disabled}
                  required
                />
              </label>
            </div>
          </fieldset>
          <label>
            最近核价日期
            <output className="date-readout">
              {snapshot?.settings?.verified_on || "尚未核价"}
            </output>
          </label>
          <label>
            报价来源说明
            <input
              name="pricing_reference"
              value={prices.pricing_reference}
              onChange={update}
              maxLength={500}
              placeholder="填写价格页面或账户报价来源"
              disabled={disabled}
              required
            />
          </label>
        </div>
        <div className="pricing-footer">
          <label className="checkbox-option">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => {
                feedback.clear();
                setConfirmed(event.target.checked);
              }}
              disabled={disabled}
            />
            <span>我已核实当前资源地区与服务价格</span>
          </label>
          <Button
            type="submit"
            icon={Save}
            variant="primary"
            busy={pending === "prices"}
            disabled={disabled || !confirmed}
          >
            保存价格配置
          </Button>
        </div>
        {feedback.saved && (
          <SaveStatus name="完整流程价格保存结果">
            完整流程价格配置已保存；保存配置不会调用模型。
          </SaveStatus>
        )}
        <p className="form-footnote">
          保存时自动记录当天为核价日期；有效期为 7
          天。保存配置不会发起模型请求。
        </p>
      </form>
    </div>
  );
}
