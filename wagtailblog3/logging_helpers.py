# wagtailblog3/logging_helpers.py

import logging
from functools import wraps

def log_exceptions(logger=None, level=logging.ERROR, message=None):
	"""装饰器：记录函数执行期间发生的异常"""
	
	def decorator(func):
		nonlocal logger
		if logger is None:
			logger = logging.getLogger(func.__module__)
		
		@wraps(func)
		def wrapper(*args, **kwargs):
			try:
				return func(*args, **kwargs)
			except Exception as e:
				log_msg = message or f"执行 {func.__name__} 时出错"
				logger.log(level, f"{log_msg}: {e}", exc_info=True)
				raise
		
		return wrapper
	
	return decorator


def get_context_logger(name, **context):
	"""获取带上下文的日志记录器"""
	logger = logging.getLogger(name)
	
	class ContextLogger:
		
		def warning(self, msg, *args, **kwargs):
			full_msg = f"{msg} - Context: {context}"
			logger.warning(full_msg, *args, **kwargs)
		
		def error(self, msg, *args, **kwargs):
			full_msg = f"{msg} - Context: {context}"
			logger.error(full_msg, *args, **kwargs)
		
		def critical(self, msg, *args, **kwargs):
			full_msg = f"{msg} - Context: {context}"
			logger.critical(full_msg, *args, **kwargs)
	
	return ContextLogger()