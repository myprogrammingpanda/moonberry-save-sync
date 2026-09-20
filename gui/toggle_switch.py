"""A small on/off switch widget, since ttk has no built-in one. Drawn on a
Canvas as a pill-shaped track with a sliding circular thumb, since Canvas is
the only stdlib Tkinter primitive that can draw a shape like this at all."""

import tkinter as tk

_WIDTH = 42
_HEIGHT = 22
_PADDING = 2


class ToggleSwitch(tk.Canvas):
    def __init__(self, parent, initial: bool, on_toggle, palette: dict):
        super().__init__(parent, width=_WIDTH, height=_HEIGHT, highlightthickness=0, bd=0)
        self.on_toggle = on_toggle
        self.state = initial
        self.palette = palette
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda e: self.configure(cursor="hand2"))
        self._draw()

    def set_palette(self, palette: dict):
        self.palette = palette
        self._draw()

    def _on_click(self, _event):
        self.state = not self.state
        self._draw()
        self.on_toggle(self.state)

    def _draw(self):
        self.delete("all")
        self.configure(background=self.palette["bg"])

        track_color = self.palette["accent"] if self.state else self.palette["border"]
        r = _HEIGHT / 2
        self.create_oval(0, 0, _HEIGHT, _HEIGHT, fill=track_color, outline=track_color)
        self.create_oval(_WIDTH - _HEIGHT, 0, _WIDTH, _HEIGHT, fill=track_color, outline=track_color)
        self.create_rectangle(r, 0, _WIDTH - r, _HEIGHT, fill=track_color, outline=track_color)

        thumb_d = _HEIGHT - 2 * _PADDING
        thumb_x = (_WIDTH - _HEIGHT + _PADDING) if self.state else _PADDING
        self.create_oval(
            thumb_x, _PADDING, thumb_x + thumb_d, _PADDING + thumb_d,
            fill="#ffffff", outline="#ffffff",
        )
