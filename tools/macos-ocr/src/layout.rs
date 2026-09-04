use std::cmp::Ordering;

use crate::cli::LayoutMode;
use crate::output::{BoundingBox, TextBlock};

pub fn sort_blocks(blocks: &mut [TextBlock], mode: LayoutMode) {
    if matches!(mode, LayoutMode::None) || blocks.len() < 2 {
        return;
    }

    let order = match mode {
        LayoutMode::None => return,
        LayoutMode::Lines | LayoutMode::ReadingOrder => line_order(blocks),
        LayoutMode::Paragraphs => paragraph_order(blocks),
        LayoutMode::Columns => column_order(blocks),
    };
    let original = blocks.to_vec();
    for (destination, source) in order.into_iter().enumerate() {
        blocks[destination] = original[source].clone();
    }
}

fn line_order(blocks: &[TextBlock]) -> Vec<usize> {
    let rows = build_rows(blocks, boxed_indices(blocks));
    let mut order = rows
        .into_iter()
        .flat_map(|mut row| {
            row.members
                .sort_by(|left, right| compare_left(blocks, *left, *right));
            row.members
        })
        .collect::<Vec<_>>();
    order.extend(unboxed_indices(blocks));
    order
}

fn paragraph_order(blocks: &[TextBlock]) -> Vec<usize> {
    let rows = build_rows(blocks, boxed_indices(blocks));
    let mut paragraphs = Vec::<Paragraph>::new();
    for row in rows {
        let belongs_to_last = paragraphs.last().is_some_and(|paragraph| {
            row.top - paragraph.bottom <= paragraph.height.max(row.height) * 1.5
        });
        if belongs_to_last {
            let paragraph = paragraphs.last_mut().expect("last paragraph exists");
            paragraph.top = paragraph.top.min(row.top);
            paragraph.bottom = paragraph.bottom.max(row.bottom);
            paragraph.height = paragraph.height.max(row.height);
            paragraph.rows.push(row);
        } else {
            paragraphs.push(Paragraph::from_row(row));
        }
    }

    let mut order = Vec::with_capacity(blocks.len());
    for paragraph in paragraphs {
        for mut row in paragraph.rows {
            row.members
                .sort_by(|left, right| compare_left(blocks, *left, *right));
            order.extend(row.members);
        }
    }
    order.extend(unboxed_indices(blocks));
    order
}

fn column_order(blocks: &[TextBlock]) -> Vec<usize> {
    let indices = boxed_indices(blocks);
    if indices.is_empty() {
        return (0..blocks.len()).collect();
    }

    let mut widths = indices
        .iter()
        .filter_map(|index| rect(blocks, *index).map(|value| value.width))
        .collect::<Vec<_>>();
    widths.sort_by(|left, right| left.total_cmp(right));
    let tolerance = widths[widths.len() / 2].max(0.01) * 1.5;

    let mut columns = Vec::<Column>::new();
    let mut sorted = indices;
    sorted.sort_by(|left, right| compare_left(blocks, *left, *right));
    for index in sorted {
        let value = rect(blocks, index).expect("boxed index has a rectangle");
        let center = value.x + value.width / 2.0;
        let matching_column = columns.iter().enumerate().min_by(|(_, left), (_, right)| {
            column_distance(left, center).total_cmp(&column_distance(right, center))
        });
        if let Some((column_index, column)) = matching_column {
            if column_distance(column, center) <= tolerance {
                let column = &mut columns[column_index];
                column.left = column.left.min(value.x);
                column.right = column.right.max(value.x + value.width);
                column.members.push(index);
                continue;
            }
        }
        columns.push(Column {
            left: value.x,
            right: value.x + value.width,
            members: vec![index],
        });
    }

    columns.sort_by(|left, right| {
        left.left
            .total_cmp(&right.left)
            .then_with(|| left.right.total_cmp(&right.right))
    });
    let mut order = Vec::with_capacity(blocks.len());
    for column in columns {
        let mut members = line_order_for_indices(blocks, column.members);
        order.append(&mut members);
    }
    order.extend(unboxed_indices(blocks));
    order
}

fn line_order_for_indices(blocks: &[TextBlock], indices: Vec<usize>) -> Vec<usize> {
    let rows = build_rows(blocks, indices);
    rows.into_iter()
        .flat_map(|mut row| {
            row.members
                .sort_by(|left, right| compare_left(blocks, *left, *right));
            row.members
        })
        .collect()
}

fn build_rows(blocks: &[TextBlock], mut indices: Vec<usize>) -> Vec<Row> {
    indices.sort_by(|left, right| compare_top(blocks, *left, *right));
    let mut rows = Vec::<Row>::new();
    for index in indices {
        let value = rect(blocks, index).expect("row ordering only receives boxed blocks");
        let matching_row = rows.iter().position(|row| same_line(row, value));
        if let Some(row_index) = matching_row {
            let row = &mut rows[row_index];
            row.top = row.top.min(value.y);
            row.bottom = row.bottom.max(value.y + value.height);
            row.height = row.height.max(value.height);
            row.members.push(index);
        } else {
            rows.push(Row {
                top: value.y,
                bottom: value.y + value.height,
                height: value.height,
                members: vec![index],
            });
        }
    }
    rows.sort_by(|left, right| left.top.total_cmp(&right.top));
    rows
}

fn same_line(row: &Row, value: &BoundingBox) -> bool {
    let overlap = (row.bottom.min(value.y + value.height) - row.top.max(value.y)).max(0.0);
    let minimum_height = row.height.min(value.height).max(f64::MIN_POSITIVE);
    let center_delta = ((row.top + row.bottom) / 2.0 - (value.y + value.height / 2.0)).abs();
    overlap / minimum_height >= 0.25 || center_delta <= row.height.max(value.height) * 0.5
}

fn compare_top(blocks: &[TextBlock], left: usize, right: usize) -> Ordering {
    match (rect(blocks, left), rect(blocks, right)) {
        (Some(left), Some(right)) => left
            .y
            .total_cmp(&right.y)
            .then_with(|| left.x.total_cmp(&right.x)),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => left.cmp(&right),
    }
}

fn compare_left(blocks: &[TextBlock], left: usize, right: usize) -> Ordering {
    match (rect(blocks, left), rect(blocks, right)) {
        (Some(left), Some(right)) => left
            .x
            .total_cmp(&right.x)
            .then_with(|| left.y.total_cmp(&right.y)),
        (Some(_), None) => Ordering::Less,
        (None, Some(_)) => Ordering::Greater,
        (None, None) => left.cmp(&right),
    }
}

fn column_distance(column: &Column, center: f64) -> f64 {
    if center < column.left {
        column.left - center
    } else if center > column.right {
        center - column.right
    } else {
        0.0
    }
}

fn boxed_indices(blocks: &[TextBlock]) -> Vec<usize> {
    (0..blocks.len())
        .filter(|index| rect(blocks, *index).is_some())
        .collect()
}

fn unboxed_indices(blocks: &[TextBlock]) -> Vec<usize> {
    (0..blocks.len())
        .filter(|index| rect(blocks, *index).is_none())
        .collect()
}

fn rect(blocks: &[TextBlock], index: usize) -> Option<&BoundingBox> {
    blocks[index].bounding_box.as_ref()
}

#[derive(Debug)]
struct Row {
    top: f64,
    bottom: f64,
    height: f64,
    members: Vec<usize>,
}

#[derive(Debug)]
struct Paragraph {
    top: f64,
    bottom: f64,
    height: f64,
    rows: Vec<Row>,
}

impl Paragraph {
    fn from_row(row: Row) -> Self {
        Self {
            top: row.top,
            bottom: row.bottom,
            height: row.height,
            rows: vec![row],
        }
    }
}

#[derive(Debug)]
struct Column {
    left: f64,
    right: f64,
    members: Vec<usize>,
}

#[cfg(test)]
mod tests {
    use super::sort_blocks;
    use crate::cli::LayoutMode;
    use crate::output::{BoundingBox, TextBlock};

    fn block(text: &str, x: f64, y: f64) -> TextBlock {
        TextBlock {
            text: text.to_owned(),
            confidence: 0.9,
            candidates: None,
            bounding_box: Some(BoundingBox {
                x,
                y,
                width: 0.1,
                height: 0.05,
            }),
        }
    }

    #[test]
    fn reading_order_sorts_blocks_by_top_then_left() {
        let mut blocks = vec![
            block("right", 0.6, 0.2),
            block("lower", 0.1, 0.7),
            block("left", 0.1, 0.2),
        ];

        sort_blocks(&mut blocks, LayoutMode::ReadingOrder);

        assert_eq!(
            blocks
                .iter()
                .map(|block| block.text.as_str())
                .collect::<Vec<_>>(),
            ["left", "right", "lower"]
        );
    }

    #[test]
    fn columns_are_read_top_to_bottom_before_the_next_column() {
        let mut blocks = vec![
            block("right-lower", 0.7, 0.6),
            block("left-lower", 0.1, 0.6),
            block("right-upper", 0.7, 0.1),
            block("left-upper", 0.1, 0.1),
        ];

        sort_blocks(&mut blocks, LayoutMode::Columns);

        assert_eq!(
            blocks
                .iter()
                .map(|block| block.text.as_str())
                .collect::<Vec<_>>(),
            ["left-upper", "left-lower", "right-upper", "right-lower"]
        );
    }

    #[test]
    fn layout_does_not_add_regions_to_blocks_without_boxes() {
        let mut blocks = vec![TextBlock {
            text: "plain".to_owned(),
            confidence: 0.9,
            bounding_box: None,
            candidates: None,
        }];

        sort_blocks(&mut blocks, LayoutMode::Lines);

        assert_eq!(blocks[0].bounding_box, None);
    }
}
