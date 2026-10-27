pub fn add(a: i64, b: i64) -> i64 {
    a + b
}

#[cfg(test)]
mod tests {
    #[test]
    fn add() {
        assert_eq!(super::add(2, 3), 5);
    }
}
