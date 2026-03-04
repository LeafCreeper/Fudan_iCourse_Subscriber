"""LLM-based course lecture summarization via ModelScope API."""

import time

from openai import OpenAI

from . import config

SYSTEM_PROMPT = """你是一个专业的课程助教。你的任务是根据用户提供的课程录音文本，生成用于学生自学和期末复习的详细结构化笔记。
1. **直接输出**：不要包含任何"好的"、"没问题"、"以下是总结"等客套话。不要输出全局课程名称大标题（由系统自动生成），直接开始总结即可。
2. **文本清洗**：语言必须通顺、逻辑清晰，严格去除口语化表达、重复句和无意义的录音识别错误等。内容可能被识别成同音字，通过学术语境修复。
3. **格式严格**：
   - 必须使用 Markdown 格式排版。
   - **标题级别限制**：只允许使用三级及后续级别的标题（即只能使用 `###`或`####`），禁止使用 `#` 和 `##`。
   - 合理使用加粗、列表、表格来组织信息，确保结构清晰。
   - 不得使用超过两级的缩进。可以适当使用bullet point列表但不得过多。不要把一段话拆成很多个用bullet point组成的短句子列表，而要尽可能用完整的段落来组织老师的讲解。
   - 出于节省空间考虑，不要使用连续的回车换行。
4. **公式规范**：所有数学公式或科学变量必须使用规范的 LaTeX 语法（行内公式用 $...$，行间公式用 $$...$$）。
5. **忠于原文与详略得当**：总结必须详略得当，长度适宜（例如，对于90分钟长度的课程，总结长度应为3000－4000字左右；135分钟的课程，应为5000字左右。输出和输入的长度压缩比例在1:8左右为宜），
包含具体的推导细节、案例、文献或者核心概念，不要过度概括，用完整连贯的段落来表示老师的意思。禁止捏造录音中未提及的内容。
6. 你需要格外注意课程中是否提及了作业、考试、签到、组队等关键事项，如果有的话，用三级标题【课程事项提醒】标注在开头。"""


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
