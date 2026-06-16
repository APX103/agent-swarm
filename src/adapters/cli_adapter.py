import asyncio
import json
import logging
import shutil
from typing import Optional

from .base import AgentBackend, AgentCapabilities, AgentResult, ProgressCallback

logger = logging.getLogger(__name__)


class CLIAdapter(AgentBackend):
    """Adapter that invokes agents via CLI subprocess."""

    def __init__(
        self,
        command: str,
        args: Optional[list[str]] = None,
        timeout: int = 300,
        workdir: Optional[str] = None,
        **kwargs,
    ) -> None:
        self.command = command
        self.args = args or []
        self.timeout = timeout
        self.workdir = workdir

    @property
    def name(self) -> str:
        return f"cli:{self.command}"

    @property
    def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(
            skills=["execute"],
            input_modes=["text"],
            output_modes=["text", "json"],
        )

    async def invoke(
        self, task: str, context: dict = None, on_progress: Optional[ProgressCallback] = None
    ) -> AgentResult:
        """Spawn a subprocess, pipe the task to stdin, and capture stdout.

        CLI agents are request/response by nature; *on_progress* is accepted
        for interface compatibility but ignored.
        """
        cmd = [self.command] + self.args
        logger.info("Invoking CLI agent: %s", " ".join(cmd))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=task.encode()),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("CLI process timed out after %ds, killing %s", self.timeout, self.command)
                proc.kill()
                await proc.wait()
                return AgentResult(
                    success=False,
                    output="",
                    error=f"Process timed out after {self.timeout}s",
                    metadata={"exit_code": proc.returncode, "timeout": True},
                )

            stdout = stdout_bytes.decode(errors="replace").strip()
            stderr = stderr_bytes.decode(errors="replace").strip()
            exit_code = proc.returncode or 0

            if exit_code != 0:
                logger.warning("CLI process exited with code %d: %s", exit_code, stderr)
                return AgentResult(
                    success=False,
                    output=stdout,
                    error=f"Exit code {exit_code}: {stderr}" if stderr else f"Exit code {exit_code}",
                    metadata={"exit_code": exit_code},
                )

            # Try to parse JSON from stdout
            parsed = None
            try:
                parsed = json.loads(stdout)
                if isinstance(parsed, dict):
                    output = parsed.get("output", parsed.get("result", stdout))
                    artifacts = parsed.get("artifacts", [])
                    return AgentResult(
                        success=True,
                        output=output if isinstance(output, str) else json.dumps(output),
                        artifacts=artifacts if isinstance(artifacts, list) else [str(artifacts)],
                        metadata={"exit_code": exit_code, "parsed_json": True},
                    )
            except (json.JSONDecodeError, ValueError):
                pass

            return AgentResult(
                success=True,
                output=stdout,
                metadata={"exit_code": exit_code, "parsed_json": False},
            )

        except FileNotFoundError:
            logger.error("CLI command not found: %s", self.command)
            return AgentResult(
                success=False,
                output="",
                error=f"Command not found: {self.command}",
            )
        except PermissionError as e:
            logger.error("Permission denied for command %s: %s", self.command, e)
            return AgentResult(
                success=False,
                output="",
                error=f"Permission denied: {e!s}",
            )
        except Exception as e:
            logger.exception("CLI adapter unexpected error")
            return AgentResult(
                success=False,
                output="",
                error=f"Unexpected error: {e!s}",
            )

    async def health_check(self) -> bool:
        """Check if the CLI command exists on the system."""
        found = shutil.which(self.command) is not None
        if not found:
            logger.debug("CLI health check failed: %s not found in PATH", self.command)
        return found
