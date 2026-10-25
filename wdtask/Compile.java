public class Compile {
    public static void main(String[] args) {
        System.exit(javax.tools.ToolProvider.getSystemJavaCompiler().run(null, null, null, args));
    }
}
