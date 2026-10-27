public class Calc {
    static int add(int a, int b) {
        return a + b;
    }

    public static void main(String[] args) {
        if (add(2, 3) != 5) {
            throw new AssertionError("add");
        }
        System.out.println("java ok");
    }
}
