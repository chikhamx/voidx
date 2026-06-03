//! Wildcard pattern matching for tool ids and file paths.
//!
//! Supports glob-style patterns: * matches any characters, ** matches across path separators.

/// Check if a string matches a wildcard pattern.
/// Supports * (single segment) and ** (multi-segment) wildcards.
pub fn matches_wildcard(pattern: &str, value: &str) -> bool {
    // Simple case: exact match
    if pattern == value {
        return true;
    }

    // ** pattern matches everything
    if pattern == "**" || pattern == "*" {
        return true;
    }

    // Decompose into segments
    let pattern_segs: Vec<&str> = pattern.split('/').collect();
    let value_segs: Vec<&str> = value.split('/').collect();

    segments_match(&pattern_segs, &value_segs, 0, 0)
}

fn segments_match(pattern: &[&str], value: &[&str], pi: usize, vi: usize) -> bool {
    if pi == pattern.len() {
        return vi == value.len();
    }

    match pattern[pi] {
        "**" => {
            // ** matches zero or more segments
            for next in vi..=value.len() {
                if segments_match(pattern, value, pi + 1, next) {
                    return true;
                }
            }
            false
        }
        "*" => {
            // * matches exactly one segment
            if vi < value.len() && segments_match(pattern, value, pi + 1, vi + 1) {
                return true;
            }
            false
        }
        seg => {
            if vi < value.len() && single_seg_match(seg, value[vi]) {
                return segments_match(pattern, value, pi + 1, vi + 1);
            }
            false
        }
    }
}

fn single_seg_match(pattern: &str, value: &str) -> bool {
    if pattern == "*" {
        return true;
    }
    if !pattern.contains('*') && !pattern.contains('?') {
        return pattern == value;
    }

    let p: Vec<char> = pattern.chars().collect();
    let v: Vec<char> = value.chars().collect();
    let mut dp = vec![vec![false; v.len() + 1]; p.len() + 1];
    dp[0][0] = true;

    for i in 0..p.len() {
        if p[i] == '*' {
            dp[i + 1][0] = dp[i][0];
        } else {
            break;
        }
    }

    for i in 0..p.len() {
        for j in 0..v.len() {
            match p[i] {
                '*' => {
                    dp[i + 1][j + 1] = dp[i][j + 1] || dp[i + 1][j];
                }
                '?' => {
                    dp[i + 1][j + 1] = dp[i][j];
                }
                c => {
                    dp[i + 1][j + 1] = dp[i][j] && c == v[j];
                }
            }
        }
    }

    dp[p.len()][v.len()]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_exact_match() {
        assert!(matches_wildcard("bash", "bash"));
        assert!(!matches_wildcard("bash", "bash2"));
    }

    #[test]
    fn test_star_matches_all() {
        assert!(matches_wildcard("*", "anything"));
        assert!(matches_wildcard("**", "a/b/c"));
    }

    #[test]
    fn test_star_segment() {
        assert!(matches_wildcard("file_*", "file_read"));
        assert!(matches_wildcard("file_*", "file_write"));
        assert!(!matches_wildcard("file_*", "bash"));
    }

    #[test]
    fn test_double_star_path() {
        assert!(matches_wildcard("mcp__**", "mcp__server__tool"));
        assert!(matches_wildcard("mcp__**__tool", "mcp__a__b__tool"));
        assert!(!matches_wildcard("mcp__**__tool", "mcp__a__b__other"));
    }

    #[test]
    fn test_question_mark() {
        assert!(matches_wildcard("file_???d", "file_read"));
        assert!(!matches_wildcard("file_???d", "file_write"));
    }
}
