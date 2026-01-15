"""
熔断器模式实现
Circuit Breaker Pattern for API Stability
"""
import time
import logging
from enum import Enum
from typing import Callable, Any


class CircuitState(Enum):
    """熔断器状态"""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """熔断器 - 防止API故障级联"""
    
    def __init__(self, failure_threshold: int = 5, timeout: int = 60, 
                 expected_exception: type = Exception, name: str = "CircuitBreaker"):
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.expected_exception = expected_exception
        self.name = name
        
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
        
        self.logger = logging.getLogger(f"CircuitBreaker.{name}")
    
    def call(self, func: Callable, *args, **kwargs) -> Any:
        """通过熔断器调用函数"""
        if self.state == CircuitState.OPEN:
            if self._should_attempt_reset():
                self.logger.info(f"[{self.name}] Attempting reset: OPEN -> HALF_OPEN")
                self.state = CircuitState.HALF_OPEN
            else:
                elapsed = time.time() - self.last_failure_time if self.last_failure_time else 0
                raise Exception(
                    f"Circuit breaker [{self.name}] is OPEN. "
                    f"Wait {self.timeout - int(elapsed)}s before retry."
                )
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
            
        except self.expected_exception as e:
            self._on_failure()
            raise e
    
    def _should_attempt_reset(self) -> bool:
        if self.last_failure_time is None:
            return True
        return time.time() - self.last_failure_time >= self.timeout
    
    def _on_success(self):
        self.failure_count = 0
        
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= 2:
                self.logger.info(f"[{self.name}] Circuit recovered: HALF_OPEN -> CLOSED")
                self.state = CircuitState.CLOSED
                self.success_count = 0
    
    def _on_failure(self):
        self.failure_count += 1
        self.last_failure_time = time.time()
        
        if self.state == CircuitState.HALF_OPEN:
            self.logger.warning(f"[{self.name}] Recovery failed: HALF_OPEN -> OPEN")
            self.state = CircuitState.OPEN
            self.success_count = 0
            
        elif self.failure_count >= self.failure_threshold:
            self.logger.error(
                f"[{self.name}] Circuit opened: {self.failure_count} failures. "
                f"Will retry after {self.timeout}s"
            )
            self.state = CircuitState.OPEN
    
    def reset(self):
        self.logger.info(f"[{self.name}] Manual reset")
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time = None
        self.state = CircuitState.CLOSED
    
    def get_state(self) -> dict:
        return {
            'name': self.name,
            'state': self.state.value,
            'failure_count': self.failure_count,
            'success_count': self.success_count,
            'last_failure_time': self.last_failure_time
        }


class CircuitBreakerManager:
    """熔断器管理器"""
    
    def __init__(self):
        self.breakers = {}
        self.logger = logging.getLogger("CircuitBreakerManager")
    
    def get_breaker(self, name: str, **kwargs) -> CircuitBreaker:
        if name not in self.breakers:
            self.breakers[name] = CircuitBreaker(name=name, **kwargs)
            self.logger.info(f"Created circuit breaker: {name}")
        return self.breakers[name]
    
    def get_all_states(self) -> dict:
        return {name: breaker.get_state() for name, breaker in self.breakers.items()}
    
    def reset_all(self):
        for breaker in self.breakers.values():
            breaker.reset()
        self.logger.info("All circuit breakers reset")


circuit_manager = CircuitBreakerManager()
