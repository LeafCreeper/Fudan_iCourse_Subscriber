"""LLM-based course lecture summarization via ModelScope API."""

import time

from openai import OpenAI

from . import config

SYSTEM_PROMPT = """你是一个专业的课程助教。你的任务是根据用户提供的课程录音文本，生成用于学生自学和期末复习的详细笔记。
1. **直接输出**：不要包含任何"好的"、"没问题"、"以下是总结"等客套话，不要输出全局课程名称大标题（由系统自动生成），直接开始总结即可。
2. **文本清洗**：语言必须通顺、逻辑清晰，严格去除口语化表达、重复句和无意义的录音识别错误等。内容可能被识别成同音字，通过学术语境修复。
3. **格式严格**：
   - 必须使用 Markdown 格式排版。
   - 标题级别限制：只允许使用三级及后续级别的标题（即只能使用 `###`、`####`或`#####`），禁止使用 `#` 和 `##`。禁止出现`##### ###`这种错误的重复标题符号。
   - 合理使用加粗、列表、表格来组织信息，确保结构清晰。
   - 不得使用超过两级的缩进。可以适当使用bullet point列表但不得过多。不要把一段话拆成很多个用bullet point组成的短句子列表，而要尽可能用完整的段落来组织老师的讲解。
   - 出于节省空间考虑，不要使用连续的回车换行，不要出现空行。
4. **公式规范**：所有数学公式或科学变量必须使用规范的 LaTeX 语法（行内公式用 $...$，行间公式用 $$...$$）。由于图床限制，latex公式中不要出现中文。
5. **忠于原文与详略得当**：总结必须详略得当，长度适宜（例如，对于90分钟长度的课程，总结长度应为3000－4000字左右；135分钟的课程，应为5000字左右。输出和输入的长度压缩比例在1:8左右为宜），
包含具体的推导细节、案例、文献或者核心概念，不要过度概括，用完整连贯的段落来表示老师的意思。禁止捏造录音中未提及的内容。
6. 你需要格外注意课程中是否提及了作业、考试、签到、组队等关键事项，如果有的话，用三级标题【课程事项提醒】标注在开头。
7. **文风示例**：以下是一个关于"梯度下降"的片段，展示了笔记总结过程中【错误的】和【正确的】的两种总结风格，请严格模仿后者。

【❌ 错误的风格】
### 梯度下降

**定义：**
- 梯度下降是一种优化算法
- 用于最小化损失函数
- 广泛应用于机器学习

**核心步骤：**
- 计算梯度
- 更新参数
- 重复迭代

**学习率：**
- 学习率决定步长
- 太大会发散
- 太小会收敛慢
- 需要调参

**类型：**
- 批量梯度下降（BGD）
- 随机梯度下降（SGD）
- 小批量梯度下降（Mini-batch GD）

---

【✅ 正确的风格】

### 梯度下降

梯度下降是最小化损失函数 $L(\theta)$ 的核心优化算法。其基本思想是沿着损失函数对参数 $\theta$ 的梯度的反方向迭代更新，每一步的更新公式为 $\theta \leftarrow \theta - \eta \nabla_\theta L(\theta)$，其中 $\eta$ 称为学习率，控制每次更新的步长大小。
学习率的选取至关重要：若 $\eta$ 过大，参数更新幅度过猛，损失函数可能在最优点附近震荡甚至发散；若 $\eta$ 过小，收敛速度极慢，训练成本大幅上升。实践中通常通过学习率调度（learning rate schedule）或自适应方法（如 Adam）来缓解这一问题。
根据每次更新时使用的样本量，梯度下降可分为三类：**批量梯度下降（BGD）** 每次使用全部训练数据，梯度估计准确但计算开销大；**随机梯度下降（SGD）** 每次仅用单个样本，更新频繁但噪声大；**小批量梯度下降（Mini-batch GD）** 则折中两者，是深度学习中最常用的形式。
---
核心区别在于：前一种的风格将一段完整的知识拆解成大量零碎的短句 bullet point，读起来像提纲而非能让人看懂的笔记，缺乏逻辑连贯性和上下文衔接；喜欢的风格用完整段落讲清楚一件事的来龙去脉，bullet point 仅在真正需要并列枚举时少量使用。"""

class Summarizer:
    """Course lecture summarizer using ModelScope OpenAI-compatible API."""

    def __init__(self):
        if not config.DASHSCOPE_API_KEY:
            raise ValueError("DASHSCOPE_API_KEY is not set")
        self.client = OpenAI(
            api_key=config.DASHSCOPE_API_KEY,
            base_url=config.LLM_BASE_URL,
        )
        self.models = list(config.LLM_MODELS)

        self._gemini_client = None
        if config.GEMINI_API_KEY:
            self._gemini_client = OpenAI(
                api_key=config.GEMINI_API_KEY,
                base_url=config.GEMINI_BASE_URL,
            )

    def _call_llm(self, client: OpenAI, model: str,
                  title: str, content: str) -> str:
        """Send a summarization request to a single model. Returns result text."""
        t0 = time.time()
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"以下是课程《{title}》的录音文本，请总结：\n\n{content}",
                },
            ],
            temperature=0.3,
            timeout=120,
        )
        result = response.choices[0].message.content
        elapsed = time.time() - t0
        print(
            f"[Summarizer] Done ({model}): {len(content)} chars input"
            f" → {len(result)} chars output in {elapsed:.0f}s"
        )
        return result

    def summarize(self, title: str, content: str) -> tuple[str, str]:
        """Summarize lecture content, trying Gemini first then ModelScope models.

        If GEMINI_API_KEY is set, Gemini is tried first. On failure (or when
        the key is absent), all ModelScope models are tried in order.

        Returns:
            (summary, model_used) tuple.

        Raises:
            RuntimeError: If all models fail.
        """
        if not content or not content.strip():
            return ("（内容为空）", "")

        errors = []

        # Primary: Gemini (when API key is available)
        if self._gemini_client:
            for model in config.GEMINI_MODELS:
                try:
                    result = self._call_llm(
                        self._gemini_client, model, title, content,
                    )
                    return (result, f"gemini/{model}")
                except Exception as e:
                    print(f"[Summarizer] gemini/{model} failed: {type(e).__name__}: {e}")
                    errors.append(f"gemini/{model}: {e}")

        # Fallback: ModelScope models
        for model in self.models:
            try:
                result = self._call_llm(self.client, model, title, content)
                return (result, model)
            except Exception as e:
                print(f"[Summarizer] {model} failed: {type(e).__name__}: {e}")
                errors.append(f"{model}: {e}")

        raise RuntimeError(
            "All LLM models failed:\n" + "\n".join(errors)
        )
