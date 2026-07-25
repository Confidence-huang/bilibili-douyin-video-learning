"""Allow `python -m cli_anything.video_learning` to use the installed CLI entry."""
from cli_anything.video_learning.video_learning_cli import main  # Reuse the console-script trigger.


if __name__ == "__main__":
    main()  # Click owns argument parsing, output, and exit codes.
