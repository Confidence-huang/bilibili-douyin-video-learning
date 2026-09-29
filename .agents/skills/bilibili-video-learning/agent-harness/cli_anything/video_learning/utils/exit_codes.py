"""
视频学习后端的退出码契约。
Agent 需要区分“等一会儿重试就好”“需要用户授权”“平台路径暂时失效”和“本地转写失败”，
否则只能把每种故障都当成“命令失败”，然后盲目重试或直接放弃。
调用示例：from cli_anything.video_learning.utils.exit_codes import EXIT_TRANSCRIPTION_FAILED
"""
from __future__ import annotations  # 支持 Python 3.10+ 的类型标注。


EXIT_SUCCESS = 0                     # 正常完成，结果已写到 stdout 或文件。
EXIT_GENERIC_FAILURE = 1             # 未分类故障；保持历史语义，调用方按“普通失败”处理。
EXIT_SHARE_PAGE_UNAVAILABLE = 20     # 分享页/取流路径不可用：可重试，或换 --download-method。
EXIT_COOKIE_PERMISSION_REQUIRED = 21  # 需要用户显式授权 Cookie 后才能继续。
EXIT_NETWORK_TIMEOUT = 22            # 网络超时或连接中断：换网络、加重试或使用代理。
EXIT_RATIO_UNAVAILABLE = 23          # 请求的画质在所有公开档位里都不可用：降档重试。
EXIT_TRANSCRIPTION_FAILED = 24       # 取流成功但本地 ASR 失败：检查运行时/模型/显存。
EXIT_SOURCES_DISAGREE = 25           # 交叉校验发现两份来源不一致：需要人工判断以哪一份为准。
EXIT_PLATFORM_VERIFICATION_REQUIRED = 26  # 平台风控/验证页：同 IP 换下载方式无效，需等待、换出口网络或授权 Cookie。
EXIT_NO_AUDIO_TRACK = 27             # 作品没有音轨（图文/纯图片）：重试与换下载方式都无意义，应改走图片 OCR。

RATIO_FAILURE_MARKERS = ("unsupported ratio", "no public play ratio worked")  # 画质失败目前只有文本可判。


# --- 本地 ASR 失败的统一类型 ---
class TranscriptionFailedError(RuntimeError):
    """取流成功、但本机转写阶段失败；与“拿不到视频”区分开，便于调用方分别处理。"""


# --- 作品没有音轨：与"取流失败""转写失败"都不同，是"这个作品本来就没有语音" ---
class NoAudioTrackError(RuntimeError):
    """图文/纯图片作品没有音轨；应改走图片 OCR，而不是重试 ASR。"""


# --- 判断一个异常属于哪一档退出码 ---
def classify_failure(exc: BaseException, ssr_error_types: tuple[type, ...] = (),
                     platform_error_types: tuple[type, ...] = (),
                     no_audio_error_types: tuple[type, ...] = ()) -> int:
    if isinstance(exc, TranscriptionFailedError):                                  # 本地 ASR 失败：重试取流没有意义
        return EXIT_TRANSCRIPTION_FAILED
    if isinstance(exc, NoAudioTrackError) or (no_audio_error_types and isinstance(exc, no_audio_error_types)):
        return EXIT_NO_AUDIO_TRACK                                                 # 没有音轨：换方式/重试都没用，改走 OCR
    if platform_error_types and isinstance(exc, platform_error_types):              # 风控/验证：比"取流不可用"更具体，先判它
        return EXIT_PLATFORM_VERIFICATION_REQUIRED
    if ssr_error_types and isinstance(exc, ssr_error_types):                        # 分享页取流失败：可以重试或换下载方式
        return EXIT_SHARE_PAGE_UNAVAILABLE
    if isinstance(exc, (TimeoutError, ConnectionError)):                            # 标准库网络异常
        return EXIT_NETWORK_TIMEOUT
    if type(exc).__module__.startswith("requests"):                                 # requests 的超时/连接异常同样归入网络档
        return EXIT_NETWORK_TIMEOUT
    message = str(exc).casefold()                                                   # 第三方异常只能按文本兜底识别
    if any(marker in message for marker in RATIO_FAILURE_MARKERS):
        return EXIT_RATIO_UNAVAILABLE
    if "timed out" in message or "timeout" in message:
        return EXIT_NETWORK_TIMEOUT
    return EXIT_GENERIC_FAILURE                                                     # 其余保持历史行为，不擅自猜测
