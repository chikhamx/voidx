//! Terminal rendering — ratatui layout and draw logic.

use crate::app::App;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Clear, List, ListItem, Paragraph, Wrap};
use ratatui::Frame;
use voidx_llm::ChatMessage;

pub fn render(frame: &mut Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),  // status bar
            Constraint::Min(0),     // transcript
            Constraint::Length(3),  // input area
        ])
        .split(frame.size());

    render_status(frame, chunks[0], app);
    render_transcript(frame, chunks[1], app);
    render_input(frame, chunks[2], app);

    // Overlay selector if active
    if app.selector_active {
        render_selector(frame, frame.size(), app);
    }
}

fn render_status(frame: &mut Frame, area: Rect, app: &App) {
    let style = if app.status.contains("Thinking") {
        Style::default().fg(Color::Yellow)
    } else if app.status.contains("Error") || app.status.contains("Select") {
        Style::default().fg(Color::Cyan)
    } else {
        Style::default().fg(Color::Gray).add_modifier(Modifier::DIM)
    };

    let text = format!(
        " voidx · {} / {} · {}",
        app.provider, app.model, app.status
    );
    frame.render_widget(Paragraph::new(text).style(style), area);
}

fn render_transcript(frame: &mut Frame, area: Rect, app: &App) {
    let mut lines: Vec<Line> = Vec::new();

    for msg in &app.system_lines {
        for line in msg.lines() {
            lines.push(Line::from(Span::styled(
                format!("  {line}"),
                Style::default().fg(Color::Gray).add_modifier(Modifier::DIM),
            )));
        }
        lines.push(Line::default());
    }

    for msg in &app.messages {
        match msg {
            ChatMessage::User { content } => {
                lines.push(Line::from(Span::styled(
                    "❯ You",
                    Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD),
                )));
                for line in content.lines() {
                    lines.push(Line::from(Span::styled(format!("  {line}"), Style::default())));
                }
                lines.push(Line::default());
            }
            ChatMessage::Assistant { content, .. } => {
                lines.push(Line::from(Span::styled(
                    "● voidx",
                    Style::default().fg(Color::Green).add_modifier(Modifier::BOLD),
                )));
                for line in content.lines() {
                    lines.push(Line::from(Span::styled(format!("  {line}"), Style::default())));
                }
                lines.push(Line::default());
            }
            _ => {}
        }
    }

    frame.render_widget(
        Paragraph::new(lines).wrap(Wrap { trim: false }),
        area,
    );
}

fn render_input(frame: &mut Frame, area: Rect, app: &App) {
    let prompt = if app.input.text().starts_with('/') {
        Span::styled("❯ ", Style::default().fg(Color::Yellow))
    } else {
        Span::styled("❯ ", Style::default().fg(Color::Cyan))
    };

    let line = Line::from(vec![prompt, Span::from(app.input.text())]);

    let block = Block::default()
        .borders(Borders::TOP)
        .border_style(Style::default().fg(Color::DarkGray));

    frame.render_widget(Paragraph::new(line).block(block), area);
}

/// Render the interactive selector overlay (for /model).
fn render_selector(frame: &mut Frame, area: Rect, app: &App) {
    if let Some((items, selected, title)) = app.selector_info() {
        let items: &[String] = items;
        let title: &str = &title;        // Centered popup
        let popup_w = items.iter().map(|i| i.len() as u16 + 4).max().unwrap_or(40).min(60);
        let popup_h = (items.len() as u16 + 3).min(20);
        let x = (area.width.saturating_sub(popup_w)) / 2;
        let y = (area.height.saturating_sub(popup_h)) / 2;
        let popup = Rect::new(x, y, popup_w, popup_h);

        frame.render_widget(Clear, popup);

        // Check if this is the API key entry mode
        if let Some(key) = app.api_key_input() {
            let masked: String = key.chars().map(|_| '•').collect();
            let text = format!("{}\n\nAPI Key: {}\n\nEnter to confirm, Esc to cancel", title, masked);
            let block = Block::default()
                .borders(Borders::ALL)
                .title(" API Key ")
                .style(Style::default().fg(Color::Yellow));
            frame.render_widget(Paragraph::new(text).block(block), popup);
            return;
        }

        // Provider / Model list
        let list_items: Vec<ListItem> = items
            .iter()
            .enumerate()
            .map(|(i, name)| {
                let style = if i == selected {
                    Style::default()
                        .fg(Color::Black)
                        .bg(Color::Cyan)
                        .add_modifier(Modifier::BOLD)
                } else {
                    Style::default()
                };
                ListItem::new(format!("  {name}  ")).style(style)
            })
            .collect();

        let list = List::new(list_items)
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(format!(" {title} "))
                    .style(Style::default().fg(Color::Yellow)),
            )
            .highlight_style(
                Style::default()
                    .fg(Color::Black)
                    .bg(Color::Cyan),
            );

        frame.render_widget(list, popup);
    }
}
