//! Input state management.

pub struct InputState {
    buffer: String,
}

impl InputState {
    pub fn new() -> Self {
        Self {
            buffer: String::new(),
        }
    }

    pub fn text(&self) -> &str {
        &self.buffer
    }

    pub fn is_empty(&self) -> bool {
        self.buffer.is_empty()
    }

    pub fn push(&mut self, c: char) {
        self.buffer.push(c);
    }

    pub fn backspace(&mut self) {
        self.buffer.pop();
    }

    pub fn take(&mut self) -> String {
        std::mem::take(&mut self.buffer)
    }

    pub fn clear(&mut self) {
        self.buffer.clear();
    }
}
