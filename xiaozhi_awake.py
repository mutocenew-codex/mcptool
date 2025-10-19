# xiaozhi_awake.py
from mcp.server.fastmcp import FastMCP
import sys
import logging
import time
import threading
from datetime import datetime, timedelta
import pygame  # 音频播放库
import os  # 用于路径处理

# 配置日志（详细输出便于排查问题）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger('XiaozhiAwake')

# 修复Windows控制台编码
if sys.platform == 'win32':
    sys.stderr.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')

# 音频配置（新增exit.wav路径）
AUDIO_FILE = os.path.abspath("static/alert.wav")  # 唤醒音频
EXIT_AUDIO_FILE = os.path.abspath("static/exit.wav")  # 待命音频（新增）

# 音频线程锁（解决多线程同时播放冲突）
audio_lock = threading.Lock()

# 初始化音频模块并检查状态
try:
    pygame.mixer.init()
    if not pygame.mixer.get_init():
        raise Exception("Pygame音频初始化失败，未检测到可用音频设备")
    logger.info("Pygame音频模块初始化成功")
except Exception as e:
    logger.error(f"音频模块初始化失败：{str(e)}，服务可能无法播放声音")


# 多任务管理器（支持并发定时任务，新增音频类型区分）
class TaskManager:
    def __init__(self):
        self.tasks = {}  # {task_id: {"timer": 定时器, "delay": 秒, "message": 消息, "status": 状态, "audio_type": 音频类型}}
        self.task_counter = 1  # 任务ID自增器

    def add_task(self, delay_seconds, message, audio_type="awake"):
        """
        添加定时任务（新增audio_type参数区分音频类型）
        :param audio_type: 音频类型，"awake"播放alert.wav，"standby"播放exit.wav
        """
        task_id = f"task_{self.task_counter}"
        self.task_counter += 1

        # 创建非阻塞定时器
        timer = threading.Timer(
            interval=delay_seconds,
            function=self._execute_task,
            args=[task_id]
        )

        # 存储任务信息（新增audio_type）
        self.tasks[task_id] = {
            "timer": timer,
            "delay": delay_seconds,
            "message": message,
            "status": "pending",
            "create_time": datetime.now().isoformat(),
            "audio_type": audio_type  # 记录音频类型
        }

        timer.start()
        self.tasks[task_id]["status"] = "running"
        logger.info(f"任务 {task_id} 创建成功（{delay_seconds}秒后执行，音频类型：{audio_type}）")
        return task_id

    def _execute_task(self, task_id):
        """执行任务：根据音频类型播放对应音频"""
        task = self.tasks.get(task_id)
        if not task:
            logger.warning(f"任务 {task_id} 不存在，跳过执行")
            return

        try:
            # 1. 根据任务的audio_type选择音频文件
            audio_file = AUDIO_FILE if task["audio_type"] == "awake" else EXIT_AUDIO_FILE
            # 2. 播放音频（加锁确保线程安全）
            self._play_audio(audio_file)
            # 3. 更新任务状态
            task["status"] = "completed"
            task["awake_time"] = datetime.now().isoformat()
            logger.info(f"任务 {task_id} 执行完成：{task['message']}（音频类型：{task['audio_type']}）")
        except Exception as e:
            task["status"] = f"failed: {str(e)}"
            logger.error(f"任务 {task_id} 执行失败：{str(e)}")

    def _play_audio(self, audio_file):
        """播放指定音频文件（修改为接收参数，支持动态切换音频）"""
        with audio_lock:
            try:
                # 检查文件是否存在
                if not os.path.exists(audio_file):
                    raise FileNotFoundError(f"音频文件不存在：{audio_file}")
                # 检查文件是否可读
                if not os.access(audio_file, os.R_OK):
                    raise PermissionError(f"无权限读取音频文件：{audio_file}")

                # 加载并播放音频
                pygame.mixer.music.load(audio_file)
                pygame.mixer.music.play()
                logger.info(f"开始播放音频：{audio_file}")

                # 等待播放完成
                while pygame.mixer.music.get_busy():
                    time.sleep(0.1)

                logger.info("音频播放完成")

            except FileNotFoundError as e:
                logger.error(str(e))
                raise
            except PermissionError as e:
                logger.error(str(e))
                raise
            except pygame.error as e:
                error_msg = f"音频播放失败（格式可能不兼容）：{str(e)}"
                logger.error(error_msg)
                raise Exception(error_msg)
            except Exception as e:
                logger.error(f"音频播放异常：{str(e)}")
                raise

    def get_task(self, task_id):
        """获取单个任务详情（包含音频类型）"""
        return self.tasks.get(task_id)

    def get_all_tasks(self):
        """获取所有任务的简化信息（包含音频类型）"""
        return {
            task_id: {
                "task_id": task_id,
                "delay": task["delay"],
                "message": task["message"],
                "status": task["status"],
                "create_time": task["create_time"],
                "awake_time": task.get("awake_time"),
                "audio_type": task["audio_type"]  # 新增：返回音频类型
            }
            for task_id, task in self.tasks.items()
        }

    def cancel_task(self, task_id):
        """取消指定任务（逻辑不变）"""
        task = self.tasks.get(task_id)
        if not task:
            return False
        if task["status"] == "running":
            task["timer"].cancel()
            task["status"] = "cancelled"
            logger.info(f"任务 {task_id} 已取消")
            return True
        return False


# 初始化MCP服务和任务管理器
mcp = FastMCP("XiaozhiAwake")
task_manager = TaskManager()


# 原有工具：设置唤醒任务（逻辑不变）
@mcp.tool()
def set_awake_timer(delay_seconds: int, message: str) -> dict:
    """
    设置定时唤醒任务（播放alert.wav）
    :param delay_seconds: 延迟时间（秒，必须≥0）
    :param message: 唤醒消息
    :return: 任务ID和状态
    """
    try:
        if not isinstance(delay_seconds, int):
            return {"success": False, "error": "延迟时间必须是整数"}
        if delay_seconds < 0:
            return {"success": False, "error": "延迟时间不能为负数"}
        if not message or not isinstance(message, str):
            return {"success": False, "error": "消息内容不能为空"}

        # 调用任务管理器，指定audio_type为"awake"
        task_id = task_manager.add_task(delay_seconds, message, audio_type="awake")
        return {
            "success": True,
            "result": {
                "task_id": task_id,
                "message": f"唤醒任务已创建（{delay_seconds}秒后执行）",
                "create_time": datetime.now().isoformat(),
                "audio_file": AUDIO_FILE
            }
        }
    except Exception as e:
        logger.error(f"创建唤醒任务失败：{str(e)}")
        return {"success": False, "error": str(e)}


# 新增工具：设置待命任务（播放exit.wav）
@mcp.tool()
def set_standby_timer(delay_seconds: int, message: str) -> dict:
    """
    设置定时待命任务（播放exit.wav，让小智AI进入待命状态）
    :param delay_seconds: 延迟时间（秒，必须≥0）
    :param message: 待命消息
    :return: 任务ID和状态
    """
    try:
        if not isinstance(delay_seconds, int):
            return {"success": False, "error": "延迟时间必须是整数"}
        if delay_seconds < 0:
            return {"success": False, "error": "延迟时间不能为负数"}
        if not message or not isinstance(message, str):
            return {"success": False, "error": "消息内容不能为空"}

        # 调用任务管理器，指定audio_type为"standby"
        task_id = task_manager.add_task(delay_seconds, message, audio_type="standby")
        return {
            "success": True,
            "result": {
                "task_id": task_id,
                "message": f"待命任务已创建（{delay_seconds}秒后执行）",
                "create_time": datetime.now().isoformat(),
                "audio_file": EXIT_AUDIO_FILE  # 返回待命音频路径
            }
        }
    except Exception as e:
        logger.error(f"创建待命任务失败：{str(e)}")
        return {"success": False, "error": str(e)}


# 原有工具：查询任务状态（自动兼容新增的待命任务）
@mcp.tool()
def get_awake_tasks(task_id: str = None) -> dict:
    """
    查询定时任务状态（支持唤醒和待命任务，返回音频类型）
    :param task_id: 可选，任务ID
    :return: 任务信息列表
    """
    try:
        if task_id:
            task = task_manager.get_task(task_id)
            if not task:
                return {"success": False, "error": f"任务ID不存在：{task_id}"}
            return {
                "success": True,
                "result": {
                    "task_id": task_id,
                    "delay": task["delay"],
                    "message": task["message"],
                    "status": task["status"],
                    "create_time": task["create_time"],
                    "awake_time": task.get("awake_time"),
                    "audio_type": task["audio_type"],  # 新增：返回音频类型
                    "audio_file": AUDIO_FILE if task["audio_type"] == "awake" else EXIT_AUDIO_FILE
                }
            }
        else:
            all_tasks = task_manager.get_all_tasks()
            return {
                "success": True,
                "result": {
                    "total_tasks": len(all_tasks),
                    "active_tasks": len([t for t in all_tasks.values() if t["status"] == "running"]),
                    "awake_tasks": len([t for t in all_tasks.values() if t["audio_type"] == "awake"]),  # 新增：唤醒任务数
                    "standby_tasks": len([t for t in all_tasks.values() if t["audio_type"] == "standby"]),  # 新增：待命任务数
                    "tasks": all_tasks
                }
            }
    except Exception as e:
        logger.error(f"查询任务失败：{str(e)}")
        return {"success": False, "error": str(e)}


# 原有工具：取消任务（自动兼容待命任务）
@mcp.tool()
def cancel_awake_task(task_id: str) -> dict:
    """
    取消指定定时任务（支持唤醒和待命任务）
    :param task_id: 任务ID
    :return: 取消结果
    """
    try:
        if not task_id:
            return {"success": False, "error": "请提供任务ID"}

        success = task_manager.cancel_task(task_id)
        if success:
            return {"success": True, "result": f"任务 {task_id} 已成功取消"}
        else:
            return {"success": False, "error": f"任务 {task_id} 不存在或已完成/取消"}
    except Exception as e:
        logger.error(f"取消任务失败：{str(e)}")
        return {"success": False, "error": str(e)}


# 原有工具：服务状态（新增待命音频检查）
@mcp.tool()
def get_awake_service_status() -> dict:
    """获取服务运行状态（包含唤醒和待命音频信息）"""
    return {
        "success": True,
        "result": {
            "service_running": True,
            "current_time": datetime.now().isoformat(),
            "uptime": str(timedelta(seconds=time.time() - mcp.start_time)),
            # 新增待命音频信息
            "awake_audio_file": AUDIO_FILE,
            "awake_audio_exists": os.path.exists(AUDIO_FILE),
            "standby_audio_file": EXIT_AUDIO_FILE,
            "standby_audio_exists": os.path.exists(EXIT_AUDIO_FILE),
            "pygame_initialized": bool(pygame.mixer.get_init()),
            "total_tasks": len(task_manager.get_all_tasks()),
            "active_tasks": len([t for t in task_manager.get_all_tasks().values() if t["status"] == "running"]),
            "awake_tasks": len([t for t in task_manager.get_all_tasks().values() if t["audio_type"] == "awake"]),
            "standby_tasks": len([t for t in task_manager.get_all_tasks().values() if t["audio_type"] == "standby"])
        }
    }


if __name__ == "__main__":
    mcp.start_time = time.time()
    logger.info(f"XiaozhiAwake服务启动，唤醒音频：{AUDIO_FILE}，待命音频：{EXIT_AUDIO_FILE}")
    mcp.run(transport="stdio")