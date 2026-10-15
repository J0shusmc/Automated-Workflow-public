from __future__ import annotations

import random
import tkinter as tk
from dataclasses import dataclass


CELL_SIZE = 24
GRID_WIDTH = 28
GRID_HEIGHT = 22
START_LENGTH = 4
START_SPEED_MS = 120
MIN_SPEED_MS = 55
SPEED_STEP = 4

BACKGROUND = "#111827"
GRID_LINE = "#1f2937"
SNAKE_HEAD = "#22c55e"
SNAKE_BODY = "#16a34a"
FOOD = "#ef4444"
TEXT = "#f9fafb"
MUTED_TEXT = "#9ca3af"


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def moved(self, direction: Point) -> Point:
        return Point(self.x + direction.x, self.y + direction.y)


UP = Point(0, -1)
DOWN = Point(0, 1)
LEFT = Point(-1, 0)
RIGHT = Point(1, 0)


class SnakeGame:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Snake")
        self.root.resizable(False, False)
        self.root.configure(bg=BACKGROUND)

        self.score_var = tk.StringVar()
        self.status_var = tk.StringVar()
        self.high_score = 0

        self._build_ui()
        self.root.bind("<KeyPress>", self.on_key_press)

        self.after_id: str | None = None
        self.reset()

    def _build_ui(self) -> None:
        header = tk.Frame(self.root, bg=BACKGROUND, padx=14, pady=12)
        header.pack(fill=tk.X)

        score = tk.Label(
            header,
            textvariable=self.score_var,
            bg=BACKGROUND,
            fg=TEXT,
            font=("TkDefaultFont", 14, "bold"),
        )
        score.pack(side=tk.LEFT)

        help_text = tk.Label(
            header,
            text="Arrows/WASD to move  |  Space to pause  |  R to restart",
            bg=BACKGROUND,
            fg=MUTED_TEXT,
            font=("TkDefaultFont", 10),
        )
        help_text.pack(side=tk.RIGHT)

        canvas_width = GRID_WIDTH * CELL_SIZE
        canvas_height = GRID_HEIGHT * CELL_SIZE
        self.canvas = tk.Canvas(
            self.root,
            width=canvas_width,
            height=canvas_height,
            bg=BACKGROUND,
            highlightthickness=0,
        )
        self.canvas.pack(padx=14, pady=(0, 14))

        self.status_label = tk.Label(
            self.root,
            textvariable=self.status_var,
            bg=BACKGROUND,
            fg=TEXT,
            font=("TkDefaultFont", 13, "bold"),
        )
        self.status_label.place(relx=0.5, rely=0.55, anchor=tk.CENTER)

    def reset(self) -> None:
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None

        center = Point(GRID_WIDTH // 2, GRID_HEIGHT // 2)
        self.snake = [Point(center.x - offset, center.y) for offset in range(START_LENGTH)]
        self.direction = RIGHT
        self.pending_direction = RIGHT
        self.food = self.spawn_food()
        self.score = 0
        self.speed_ms = START_SPEED_MS
        self.paused = False
        self.game_over = False

        self.update_score()
        self.status_var.set("")
        self.draw()
        self.schedule_tick()

    def spawn_food(self) -> Point:
        occupied = set(self.snake)
        free_cells = [
            Point(x, y)
            for x in range(GRID_WIDTH)
            for y in range(GRID_HEIGHT)
            if Point(x, y) not in occupied
        ]
        return random.choice(free_cells)

    def schedule_tick(self) -> None:
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
        self.after_id = self.root.after(self.speed_ms, self.tick)

    def tick(self) -> None:
        self.after_id = None
        if self.paused or self.game_over:
            return

        self.direction = self.pending_direction
        next_head = self.snake[0].moved(self.direction)

        if self.hit_wall(next_head) or self.hit_self(next_head):
            self.end_game()
            return

        self.snake.insert(0, next_head)
        if next_head == self.food:
            self.score += 1
            self.high_score = max(self.high_score, self.score)
            self.speed_ms = max(MIN_SPEED_MS, self.speed_ms - SPEED_STEP)
            self.food = self.spawn_food()
            self.update_score()
        else:
            self.snake.pop()

        self.draw()
        self.schedule_tick()

    def hit_wall(self, point: Point) -> bool:
        return point.x < 0 or point.x >= GRID_WIDTH or point.y < 0 or point.y >= GRID_HEIGHT

    def hit_self(self, point: Point) -> bool:
        return point in self.snake

    def end_game(self) -> None:
        self.game_over = True
        self.after_id = None
        self.status_var.set("Game over - press R to restart")
        self.draw()

    def update_score(self) -> None:
        self.score_var.set(f"Score: {self.score}   High Score: {self.high_score}")

    def draw(self) -> None:
        self.canvas.delete("all")
        self.draw_grid()
        self.draw_food()
        self.draw_snake()

    def draw_grid(self) -> None:
        width = GRID_WIDTH * CELL_SIZE
        height = GRID_HEIGHT * CELL_SIZE
        for x in range(0, width, CELL_SIZE):
            self.canvas.create_line(x, 0, x, height, fill=GRID_LINE)
        for y in range(0, height, CELL_SIZE):
            self.canvas.create_line(0, y, width, y, fill=GRID_LINE)

    def draw_food(self) -> None:
        padding = 4
        self.draw_cell(self.food, FOOD, padding=padding, oval=True)

    def draw_snake(self) -> None:
        for index, segment in enumerate(self.snake):
            color = SNAKE_HEAD if index == 0 else SNAKE_BODY
            self.draw_cell(segment, color, padding=2)

    def draw_cell(self, point: Point, color: str, padding: int = 0, oval: bool = False) -> None:
        x1 = point.x * CELL_SIZE + padding
        y1 = point.y * CELL_SIZE + padding
        x2 = (point.x + 1) * CELL_SIZE - padding
        y2 = (point.y + 1) * CELL_SIZE - padding
        draw = self.canvas.create_oval if oval else self.canvas.create_rectangle
        draw(x1, y1, x2, y2, fill=color, outline="")

    def on_key_press(self, event: tk.Event) -> None:
        key = event.keysym.lower()
        if key in {"r", "return"}:
            self.reset()
            return
        if key == "space":
            self.toggle_pause()
            return
        if self.paused or self.game_over:
            return

        new_direction = {
            "up": UP,
            "w": UP,
            "down": DOWN,
            "s": DOWN,
            "left": LEFT,
            "a": LEFT,
            "right": RIGHT,
            "d": RIGHT,
        }.get(key)

        if new_direction and not self.is_reverse(new_direction):
            self.pending_direction = new_direction

    def toggle_pause(self) -> None:
        if self.game_over:
            return

        self.paused = not self.paused
        self.status_var.set("Paused" if self.paused else "")
        if self.paused and self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        elif not self.paused:
            self.schedule_tick()

    def is_reverse(self, new_direction: Point) -> bool:
        return (
            new_direction.x == -self.direction.x
            and new_direction.y == -self.direction.y
        )


def main() -> None:
    root = tk.Tk()
    SnakeGame(root)
    root.mainloop()


if __name__ == "__main__":
    main()
