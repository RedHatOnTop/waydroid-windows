import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.List;

/**
 * Task control for wlproxy, run as the shell user through app_process:
 *
 *   CLASSPATH=/data/local/tmp/wdtask.dex app_process / WdTask
 *
 * One command per stdin line, each answered by result lines and a "." line:
 *   list                       -> "taskId package mode visible left top right bottom minWidth minHeight"
 *                                 per task; the minimums are the activity's declared ones, -1 if none
 *   bounds PACKAGE L T R B     -> freeform at those bounds (leaves fullscreen if needed)
 *   fullscreen PACKAGE         -> fullscreen windowing mode, the same as Android's caption maximize
 *
 * `cmd activity task resize` only resizes freeform tasks and no shell command changes the
 * windowing mode, so this goes through the window organizer the way SystemUI's shell does.
 * Everything is reflective because the classes are hidden API; no SDK jar is needed to build.
 */
public final class WdTask {
    private static final int FULLSCREEN = 1;
    private static final int FREEFORM = 5;
    private static Object atm;
    private static Object organizer;

    public static void main(String[] args) throws Exception {
        atm = Class.forName("android.app.ActivityTaskManager").getMethod("getService").invoke(null);
        organizer = atm.getClass().getMethod("getWindowOrganizerController").invoke(atm);
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        System.out.println("ready");
        System.out.flush();
        for (String line; (line = in.readLine()) != null; ) {
            String[] a = line.trim().split("\\s+");
            try {
                handle(a);
            } catch (Throwable t) {
                Throwable cause = t.getCause() != null ? t.getCause() : t;
                System.out.println("err " + cause);
            }
            System.out.println(".");
            System.out.flush();
        }
    }

    private static void handle(String[] a) throws Exception {
        switch (a[0]) {
            case "list":
                for (Object t : tasks()) {
                    int[] r = bounds(t);
                    int[] min = minSize(t);
                    System.out.println(field(t, "taskId") + " " + pkg(t) + " " + mode(t) + " " + field(t, "isVisible")
                            + " " + r[0] + " " + r[1] + " " + r[2] + " " + r[3] + " " + min[0] + " " + min[1]);
                }
                break;
            case "bounds": {
                Object t = find(a[1]);
                int[] r = {Integer.parseInt(a[2]), Integer.parseInt(a[3]), Integer.parseInt(a[4]), Integer.parseInt(a[5])};
                apply(t, mode(t) == FREEFORM ? null : FREEFORM, r);
                System.out.println("ok " + field(t, "taskId"));
                break;
            }
            case "fullscreen": {
                Object t = find(a[1]);
                if (mode(t) != FULLSCREEN) {
                    apply(t, FULLSCREEN, null);
                }
                System.out.println("ok " + field(t, "taskId"));
                break;
            }
            default:
                System.out.println("err unknown command " + a[0]);
        }
    }

    private static List<?> tasks() throws Exception {
        for (Method m : atm.getClass().getMethods()) {
            if (!m.getName().equals("getTasks")) {
                continue;
            }
            Class<?>[] p = m.getParameterTypes();
            Object[] v = new Object[p.length];
            boolean first = true;
            for (int i = 0; i < p.length; i++) {
                if (p[i] == int.class) {
                    v[i] = first ? 100 : -1;
                    first = false;
                } else if (p[i] == boolean.class) {
                    v[i] = false;
                }
            }
            return (List<?>) m.invoke(atm, v);
        }
        throw new IllegalStateException("no getTasks");
    }

    private static Object find(String pkg) throws Exception {
        Object fallback = null;
        for (Object t : tasks()) {
            if (!pkg.equals(pkg(t))) {
                continue;
            }
            if (Boolean.TRUE.equals(field(t, "isVisible"))) {
                return t;
            }
            if (fallback == null) {
                fallback = t;
            }
        }
        if (fallback == null) {
            throw new IllegalArgumentException("no task for " + pkg);
        }
        return fallback;
    }

    private static void apply(Object task, Integer mode, int[] r) throws Exception {
        Object token = field(task, "token");
        Class<?> wctClass = Class.forName("android.window.WindowContainerTransaction");
        Class<?> tokenClass = Class.forName("android.window.WindowContainerToken");
        Class<?> rectClass = Class.forName("android.graphics.Rect");
        Object wct = wctClass.getConstructor().newInstance();
        if (mode != null) {
            wctClass.getMethod("setWindowingMode", tokenClass, int.class).invoke(wct, token, mode);
        }
        Object rect = r == null ? rectClass.getConstructor().newInstance()
                : rectClass.getConstructor(int.class, int.class, int.class, int.class).newInstance(r[0], r[1], r[2], r[3]);
        wctClass.getMethod("setBounds", tokenClass, rectClass).invoke(wct, token, rect);
        organizer.getClass().getMethod("applyTransaction", wctClass).invoke(organizer, wct);
    }

    private static String pkg(Object task) throws Exception {
        Object c = field(task, "baseActivity");
        if (c == null) {
            c = field(task, "topActivity");
        }
        return c == null ? "" : (String) c.getClass().getMethod("getPackageName").invoke(c);
    }

    private static int mode(Object task) throws Exception {
        Object wc = field(field(task, "configuration"), "windowConfiguration");
        return (Integer) wc.getClass().getMethod("getWindowingMode").invoke(wc);
    }

    private static int[] bounds(Object task) throws Exception {
        Object wc = field(field(task, "configuration"), "windowConfiguration");
        Object r = wc.getClass().getMethod("getBounds").invoke(wc);
        return new int[] {(Integer) field(r, "left"), (Integer) field(r, "top"), (Integer) field(r, "right"), (Integer) field(r, "bottom")};
    }

    private static int[] minSize(Object task) throws Exception {
        Object info = field(task, "topActivityInfo");
        Object layout = info == null ? null : field(info, "windowLayout");
        if (layout == null) {
            return new int[] {-1, -1};
        }
        return new int[] {(Integer) field(layout, "minWidth"), (Integer) field(layout, "minHeight")};
    }

    private static Object field(Object o, String name) throws Exception {
        for (Class<?> c = o.getClass(); c != null; c = c.getSuperclass()) {
            try {
                Field f = c.getDeclaredField(name);
                f.setAccessible(true);
                return f.get(o);
            } catch (NoSuchFieldException e) {
                // keep looking in the superclass
            }
        }
        throw new NoSuchFieldException(name);
    }
}
