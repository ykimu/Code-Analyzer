namespace geo {

class Shape {
public:
  virtual double area();
};

class Circle : public Shape {
public:
  double area();
private:
  double r;
};

double Shape::area() {
  return 0.0;
}

double Circle::area() {
  double a = 3.14 * r;
  return a;
}

}  // namespace geo
